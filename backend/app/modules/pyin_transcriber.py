"""v1 real transcription implementation: a pYIN-based `PitchTracker`, a
range-clamp `OctaveCorrector`, and a `Transcriber` facade composing them.

Design notes (see docs/ARCHITECTURE.md, transcription.py):
  * `audio` is raw 16-bit signed little-endian mono PCM samples (NOT a WAV
    file with its own header) - `sample_rate` is passed separately because
    the caller already knows it. This is the simplest contract for a
    frontend that's already decoded/resampled audio before upload; a WAV-
    file-in variant can wrap this later without changing the contract.
  * Note SEGMENTATION - deciding where one note ends and the next begins -
    is the hard part for a bowed string, not pitch estimation itself.
    Amplitude-based onset detection (`librosa.onset.onset_detect`) is
    accurate for separately-bowed notes, but misses the boundary entirely
    between SLURRED notes (same bow stroke, no new attack transient to
    detect). Validated against a real recording (Rabbath Etude No. 1,
    played freely/not to a click): pure onset detection under-segmented a
    62-note passage by roughly a third, concentrated exactly at the
    slurred spots. See `_split_oversized_segments`.
  * `_split_oversized_segments`: a real fix for the slur problem that
    doesn't require knowing in advance which notes are slurred. A segment
    unusually long relative to the piece's own median note duration almost
    certainly swallowed more than one note (a slur ate the internal
    onset); those get sub-split by pitch-CHANGE alone (the only signal a
    slur leaves - no new attack, but the pitch still moves), while
    everything else keeps the more pitch-accurate pure-onset boundaries.
    Tried the reverse (smooth/segment everything by pitch-change) first -
    it caught more note events but was LESS pitch-accurate and introduced
    a large systematic onset-reporting lag (the smoothing needed to reject
    noise also delays exactly when a boundary gets reported); the hybrid
    (onset-first, pitch-split only where onsets clearly failed) beat both
    the pure-onset and pure-pitch-change approaches on the real recording.
  * Near-zero-confidence segments (`MIN_CONFIDENCE`) are dropped before
    being reported as notes at all, not just down-weighted - validated as
    real pre-performance noise floor (bow/tuning noise before the piece
    starts) on a real recording, not real musical content.
  * `RangeClampOctaveCorrector` is deliberately the simplest policy named
    as the v1 starting point in ARCHITECTURE.md's risk area #1 - shifts a
    note by whole octaves until it falls inside
    `[config.min_midi, config.max_midi]`. Does NOT attempt the more
    sophisticated median-smoothing/overtone heuristics also mentioned
    there; that's future work if this proves insufficient.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import medfilt

from .transcription import (
    DetectedNote,
    OctaveCorrector,
    PitchTracker,
    Transcriber,
    Transcription,
    TranscriptionConfig,
)

# Below this mean per-note confidence, treat a segment as noise floor (bow
# settling, tuning, room noise before/after playing) and drop it entirely
# rather than reporting it as a low-confidence note. Calibrated against a
# real recording where genuine pre-performance noise sat at ~0.01-0.02
# while real (if quiet) playing was consistently higher.
MIN_CONFIDENCE = 0.03

# A segment longer than this multiple of the piece's median segment
# duration is treated as a probable slur (more than one note, no internal
# onset detected) and gets sub-split by pitch-change. Chosen empirically;
# not validated across pieces/tempos beyond the one real recording so far.
OVERSIZED_FACTOR = 2.2

# A pitch-change sub-segment inside `_split_oversized_segments` shorter than
# this many analysis frames is treated as a transition artifact, not a real
# note, and gets merged into a neighboring run instead of being reported as
# its own note. Root-caused against a real recording (Rabbath Etude No. 1):
# the medfilt(kernel=3) denoise in `_split_oversized_segments` still passes
# through short (1-5 frame / 10-50ms) runs exactly at a slurred pitch
# transition (a finger sliding/rolling between positions, or a bow-noise
# transient at the join) - those runs get `_pitch_stats`'d and reported as
# real notes with almost no averaging to reject the transition noise,
# frequently landing a semitone or two into the NEIGHBORING octave region
# (which is why `RangeClampOctaveCorrector` can't catch it - the bad
# estimate is already in-range, just wrong). Accounted for ~73% (24/33) of
# the near-octave-but-not-exactly-12-semitone error cluster found in the
# full-etude validation run; see ARCHITECTURE.md risk area #1. 4 frames
# (~40ms at the default 10ms hop) is short even for a fast passing tone at
# a brisk tempo, so this should not eat real short notes - see
# `test_split_oversized_segments_merges_short_transition_fragment`.
MIN_SPLIT_RUN_FRAMES = 4


def _frame_length_for(frame_size_ms: float, sample_rate: int, fmin_hz: float) -> int:
    """pYIN needs roughly 2 periods of fmin to fit in one analysis frame or
    it emits a runtime warning and degrades at the low end - the exact
    tradeoff `TranscriptionConfig.frame_size_ms` exists to make explicit
    (see transcription.py's docstring). This takes whichever is larger:
    the caller's requested frame size, or the minimum needed for fmin.
    """
    requested = int(sample_rate * frame_size_ms / 1000.0)
    min_needed = int(2.2 * sample_rate / fmin_hz)  # a little headroom over the bare minimum
    frame_length = max(requested, min_needed)
    # librosa wants a power-of-2-friendly-ish size; round up to the next
    # multiple of 256 rather than requiring an exact power of 2.
    return ((frame_length + 255) // 256) * 256


class PyinPitchTracker(PitchTracker):
    """Raw pitch + onset detection. Emits notes WITHOUT octave correction
    (that's `RangeClampOctaveCorrector`'s job) - see transcription.py's
    contract for why these are kept separate.
    """

    def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        config: TranscriptionConfig,
    ) -> Transcription:
        if len(audio) < 4:
            return Transcription(notes=[], config=config, sample_rate=sample_rate, duration_s=0.0)

        y = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        duration_s = len(y) / sample_rate

        import librosa  # deferred: heavy import, only needed when actually tracking

        fmin_hz = float(librosa.midi_to_hz(config.min_midi))
        fmax_hz = float(librosa.midi_to_hz(config.max_midi))
        frame_length = _frame_length_for(config.frame_size_ms, sample_rate, fmin_hz)
        hop_length = max(1, int(sample_rate * config.hop_size_ms / 1000.0))

        f0, voiced_flag, voiced_prob = librosa.pyin(
            y,
            fmin=fmin_hz,
            fmax=fmax_hz,
            sr=sample_rate,
            frame_length=frame_length,
            hop_length=hop_length,
        )
        times = librosa.times_like(f0, sr=sample_rate, hop_length=hop_length)
        voiced_flag = np.asarray(voiced_flag, dtype=bool)

        onsets = librosa.onset.onset_detect(
            y=y, sr=sample_rate, units="time", backtrack=True, hop_length=hop_length
        )
        boundaries = sorted(set(float(t) for t in onsets if t < duration_s) | {duration_s})
        if not boundaries or boundaries[0] > 1e-9:
            boundaries = [0.0] + boundaries

        raw_segments = _onset_segments(times, f0, voiced_flag, voiced_prob, boundaries)
        split_segments = _split_oversized_segments(raw_segments, times, f0, voiced_flag, voiced_prob)
        kept = [s for s in split_segments if s.confidence >= MIN_CONFIDENCE]
        kept.sort(key=lambda s: s.onset_s)

        notes = [
            DetectedNote(
                index=i,
                midi=s.midi,
                cents_offset=s.cents_offset,
                onset_s=s.onset_s,
                offset_s=s.offset_s,
                confidence=s.confidence,
            )
            for i, s in enumerate(kept)
        ]
        return Transcription(notes=notes, config=config, sample_rate=sample_rate, duration_s=duration_s)


class _Segment:
    __slots__ = ("onset_s", "offset_s", "midi", "cents_offset", "confidence")

    def __init__(self, onset_s: float, offset_s: float, midi: int, cents_offset: float, confidence: float):
        self.onset_s = onset_s
        self.offset_s = offset_s
        self.midi = midi
        self.cents_offset = cents_offset
        self.confidence = confidence


def _pitch_stats(seg_f0: np.ndarray, seg_conf: np.ndarray) -> tuple[int, float, float]:
    """Median-pitch summary of a voiced frame slice -> (midi, cents_offset, confidence)."""
    median_f0 = float(np.median(seg_f0))
    midi_exact = 69.0 + 12.0 * np.log2(max(median_f0, 1e-6) / 440.0)
    midi_round = int(round(midi_exact))
    cents_offset = (midi_exact - midi_round) * 100.0
    confidence = float(np.mean(seg_conf))
    return midi_round, cents_offset, confidence


def _onset_segments(
    times: np.ndarray,
    f0: np.ndarray,
    voiced_flag: np.ndarray,
    voiced_prob: np.ndarray,
    boundaries: list[float],
) -> list[_Segment]:
    segments: list[_Segment] = []
    for i in range(len(boundaries) - 1):
        t0, t1 = boundaries[i], boundaries[i + 1]
        mask = (times >= t0) & (times < t1) & voiced_flag
        if mask.sum() < 2:
            continue
        midi, cents, conf = _pitch_stats(f0[mask], voiced_prob[mask])
        segments.append(_Segment(t0, t1, midi, cents, conf))
    return segments


def _split_oversized_segments(
    segments: list[_Segment],
    times: np.ndarray,
    f0: np.ndarray,
    voiced_flag: np.ndarray,
    voiced_prob: np.ndarray,
) -> list[_Segment]:
    if not segments:
        return segments

    durations = [s.offset_s - s.onset_s for s in segments]
    median_dur = float(np.median(durations))
    if median_dur <= 0:
        return segments
    threshold = OVERSIZED_FACTOR * median_dur

    result: list[_Segment] = []
    for seg in segments:
        if (seg.offset_s - seg.onset_s) <= threshold:
            result.append(seg)
            continue

        mask = (times >= seg.onset_s) & (times < seg.offset_s) & voiced_flag
        if mask.sum() < 2:
            result.append(seg)
            continue

        seg_t = times[mask]
        seg_f0 = f0[mask]
        seg_conf = voiced_prob[mask]

        midi_raw = 69.0 + 12.0 * np.log2(np.maximum(seg_f0, 1e-6) / 440.0)
        # Light denoise (kernel=3) before rounding to semitone buckets -
        # kills single-frame jitter without the heavier lag a wider window
        # would introduce (see module docstring: that tradeoff is why the
        # pure pitch-smoothed approach lost on timing accuracy overall).
        midi_smooth = medfilt(midi_raw, kernel_size=3) if len(midi_raw) >= 3 else midi_raw
        rounded = np.round(midi_smooth)

        j = 0
        n = len(rounded)
        runs: list[list[int]] = []  # [start_idx, end_idx) into seg_t/seg_f0/seg_conf
        while j < n:
            k = j
            while k < n and rounded[k] == rounded[j]:
                k += 1
            runs.append([j, k])
            j = k

        # Merge any run shorter than MIN_SPLIT_RUN_FRAMES into its longer
        # neighbor. Short runs here are almost always a slurred pitch-
        # transition artifact rather than a real note - see
        # MIN_SPLIT_RUN_FRAMES's docstring. Re-check from the start after
        # each merge since merging can make a previously-fine neighbor the
        # new shortest run's target, or combine two short runs together.
        changed = True
        while changed and len(runs) > 1:
            changed = False
            for idx, (a, b) in enumerate(runs):
                if (b - a) >= MIN_SPLIT_RUN_FRAMES:
                    continue
                left = runs[idx - 1] if idx > 0 else None
                right = runs[idx + 1] if idx < len(runs) - 1 else None
                if left is not None and right is not None:
                    target = left if (left[1] - left[0]) >= (right[1] - right[0]) else right
                elif left is not None:
                    target = left
                elif right is not None:
                    target = right
                else:
                    break  # only run left, nothing to merge into
                if target is left:
                    left[1] = b
                else:
                    right[0] = a
                runs.pop(idx)
                changed = True
                break

        for a, b in runs:
            sub_t0 = float(seg_t[a])
            sub_t1 = float(seg_t[b]) if b < n else seg.offset_s
            if sub_t1 > sub_t0:
                midi, cents, conf = _pitch_stats(seg_f0[a:b], seg_conf[a:b])
                result.append(_Segment(sub_t0, sub_t1, midi, cents, conf))

    return result


class RangeClampOctaveCorrector(OctaveCorrector):
    """Simplest v1 octave-correction policy (see ARCHITECTURE.md risk area
    #1): shift a note by whole octaves until it lands inside
    `[config.min_midi, config.max_midi]`. A note already in range is left
    untouched. Does not attempt median-smoothing across neighbors or
    overtone-jump heuristics - those are real, more effective policies for
    later, deliberately not attempted here.
    """

    def correct(self, transcription: Transcription, config: TranscriptionConfig) -> Transcription:
        corrected_notes = []
        for note in transcription.notes:
            midi = note.midi
            shifted = False
            while midi < config.min_midi:
                midi += 12
                shifted = True
            while midi > config.max_midi:
                midi -= 12
                shifted = True
            if shifted:
                corrected_notes.append(note.model_copy(update={"midi": midi, "octave_corrected": True}))
            else:
                corrected_notes.append(note)
        return transcription.model_copy(update={"notes": corrected_notes})


class PyinTranscriber(Transcriber):
    """v1 `Transcriber` facade: `PyinPitchTracker` + `RangeClampOctaveCorrector`."""

    def __init__(
        self,
        pitch_tracker: PitchTracker | None = None,
        octave_corrector: OctaveCorrector | None = None,
    ):
        self._pitch_tracker = pitch_tracker or PyinPitchTracker()
        self._octave_corrector = octave_corrector or RangeClampOctaveCorrector()

    def run(
        self,
        audio: bytes,
        sample_rate: int,
        config: TranscriptionConfig | None = None,
    ) -> Transcription:
        cfg = config or TranscriptionConfig()
        raw = self._pitch_tracker.transcribe(audio, sample_rate, cfg)
        return self._octave_corrector.correct(raw, cfg)
