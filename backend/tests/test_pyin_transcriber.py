"""Tests for the v1 real transcription implementation (pyin_transcriber.py).

Uses synthesized sine-wave audio (not the developer's real recording,
which isn't committed - it's personal performance audio, and wouldn't be
a stable/portable CI fixture anyway). Sine waves can't validate real-world
segmentation quality against actual bowed-string audio - that validation
happened separately against a real recording (see git history / STATUS.md
for the before/after numbers this module's design choices are based on).
What these tests validate is that the CODE does what its docstring claims
on clean, controllable input: correct pitch detection, correct onset
segmentation for clearly separated notes, correct octave clamping, and
correct composition in the Transcriber facade.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.modules.pyin_transcriber import (
    MIN_CONFIDENCE,
    MIN_SPLIT_RUN_FRAMES,
    PyinPitchTracker,
    PyinTranscriber,
    RangeClampOctaveCorrector,
    _frame_length_for,
    _split_oversized_segments,
    _Segment,
)
from app.modules.transcription import DetectedNote, PitchTracker, Transcription, TranscriptionConfig

SAMPLE_RATE = 22050  # lower than real 44.1/48k - keeps pyin fast in tests


def _sine_pcm16(freq_hz: float, duration_s: float, sample_rate: int = SAMPLE_RATE, amplitude: float = 0.6) -> bytes:
    t = np.arange(int(duration_s * sample_rate)) / sample_rate
    wave = amplitude * np.sin(2 * np.pi * freq_hz * t)
    return (wave * 32767).astype(np.int16).tobytes()


def _silence_pcm16(duration_s: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    return np.zeros(int(duration_s * sample_rate), dtype=np.int16).tobytes()


def _midi_to_hz(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


@pytest.fixture
def config() -> TranscriptionConfig:
    return TranscriptionConfig(frame_size_ms=60.0, hop_size_ms=10.0, min_midi=28, max_midi=67)


def test_frame_length_for_respects_fmin_minimum() -> None:
    # A very low fmin (e.g. MIDI 23, ~30Hz) needs a bigger frame than a
    # 60ms request at typical sample rates - the helper must bump up, not
    # silently use a too-short frame (the real bug hit earlier this
    # project: librosa warns and degrades quality at the low end).
    small_request_frame = _frame_length_for(frame_size_ms=20.0, sample_rate=48000, fmin_hz=30.0)
    naive_20ms = int(48000 * 20.0 / 1000.0)
    assert small_request_frame > naive_20ms

    # A generous request for a reasonably high fmin should just use the
    # request (already well above the fmin-driven minimum).
    generous_frame = _frame_length_for(frame_size_ms=100.0, sample_rate=48000, fmin_hz=200.0)
    assert generous_frame >= int(48000 * 100.0 / 1000.0)


def test_single_sustained_note_detected_at_correct_pitch(config: TranscriptionConfig) -> None:
    midi = 45  # A2
    audio = _sine_pcm16(_midi_to_hz(midi), duration_s=1.0)
    tracker = PyinPitchTracker()

    transcription = tracker.transcribe(audio, SAMPLE_RATE, config)

    assert transcription.notes, "expected at least one detected note"
    # Take the highest-confidence note as "the" detection - a clean single
    # sustained tone may still get split at its very start/decay edges.
    best = max(transcription.notes, key=lambda n: n.confidence)
    assert best.midi == midi
    assert best.confidence > MIN_CONFIDENCE


def test_two_separately_bowed_notes_are_segmented_apart(config: TranscriptionConfig) -> None:
    # A real silence gap plus a pitch change gives onset_detect a clean,
    # unambiguous attack to find - this is the "easy" case the module
    # docstring says amplitude onsets handle well (the hard case, slurs,
    # can't be synthesized this simply and was validated on real audio).
    note_a = _sine_pcm16(_midi_to_hz(41), duration_s=0.6)  # F2
    gap = _silence_pcm16(0.05)
    note_b = _sine_pcm16(_midi_to_hz(48), duration_s=0.6)  # C3
    audio = note_a + gap + note_b

    tracker = PyinPitchTracker()
    transcription = tracker.transcribe(audio, SAMPLE_RATE, config)

    detected_midis = {n.midi for n in transcription.notes if n.confidence > 0.1}
    assert 41 in detected_midis
    assert 48 in detected_midis


def test_silence_produces_no_notes(config: TranscriptionConfig) -> None:
    audio = _silence_pcm16(1.0)
    tracker = PyinPitchTracker()

    transcription = tracker.transcribe(audio, SAMPLE_RATE, config)

    assert transcription.notes == []


def test_empty_audio_returns_empty_transcription(config: TranscriptionConfig) -> None:
    tracker = PyinPitchTracker()
    transcription = tracker.transcribe(b"", SAMPLE_RATE, config)

    assert transcription.notes == []
    assert transcription.duration_s == 0.0


# --------------------------------------------------------------------------- #
# RangeClampOctaveCorrector - pure unit tests, no audio needed.
# --------------------------------------------------------------------------- #

def _detected(midi: int) -> DetectedNote:
    return DetectedNote(index=0, midi=midi, onset_s=0.0, offset_s=1.0, confidence=0.9)


def test_note_in_range_is_untouched() -> None:
    config = TranscriptionConfig(min_midi=28, max_midi=67)
    note = _detected(45)
    transcription = Transcription(notes=[note], config=config, sample_rate=44100, duration_s=1.0)

    corrected = RangeClampOctaveCorrector().correct(transcription, config)

    assert corrected.notes[0].midi == 45
    assert corrected.notes[0].octave_corrected is False


def test_note_below_range_is_shifted_up_by_octaves() -> None:
    config = TranscriptionConfig(min_midi=28, max_midi=67)
    note = _detected(10)  # two octaves below min_midi=28... let's check: 28-10=18, not a clean multiple of 12
    transcription = Transcription(notes=[note], config=config, sample_rate=44100, duration_s=1.0)

    corrected = RangeClampOctaveCorrector().correct(transcription, config)

    result_midi = corrected.notes[0].midi
    assert config.min_midi <= result_midi <= config.max_midi
    assert (result_midi - 10) % 12 == 0  # only ever shifted by whole octaves
    assert corrected.notes[0].octave_corrected is True


def test_note_above_range_is_shifted_down_by_octaves() -> None:
    config = TranscriptionConfig(min_midi=28, max_midi=67)
    note = _detected(90)
    transcription = Transcription(notes=[note], config=config, sample_rate=44100, duration_s=1.0)

    corrected = RangeClampOctaveCorrector().correct(transcription, config)

    result_midi = corrected.notes[0].midi
    assert config.min_midi <= result_midi <= config.max_midi
    assert (90 - result_midi) % 12 == 0
    assert corrected.notes[0].octave_corrected is True


# --------------------------------------------------------------------------- #
# PyinTranscriber facade - verifies composition, not pitch-tracking quality.
# --------------------------------------------------------------------------- #

class _FakePitchTracker(PitchTracker):
    """Returns a fixed out-of-range note so the facade test can verify
    octave correction actually runs, without needing real audio."""

    def transcribe(self, audio: bytes, sample_rate: int, config: TranscriptionConfig) -> Transcription:
        note = DetectedNote(index=0, midi=90, onset_s=0.0, offset_s=1.0, confidence=0.9)
        return Transcription(notes=[note], config=config, sample_rate=sample_rate, duration_s=1.0)


def test_transcriber_facade_composes_tracker_and_corrector() -> None:
    transcriber = PyinTranscriber(pitch_tracker=_FakePitchTracker())
    config = TranscriptionConfig(min_midi=28, max_midi=67)

    result = transcriber.run(b"unused", sample_rate=44100, config=config)

    assert len(result.notes) == 1
    assert config.min_midi <= result.notes[0].midi <= config.max_midi
    assert result.notes[0].octave_corrected is True


def test_transcriber_facade_default_config() -> None:
    # run() without an explicit config should use TranscriptionConfig()'s
    # defaults, not crash.
    transcriber = PyinTranscriber(pitch_tracker=_FakePitchTracker())
    result = transcriber.run(b"unused", sample_rate=44100)
    assert len(result.notes) == 1


# --------------------------------------------------------------------------- #
# _split_oversized_segments - direct unit tests against synthetic frame
# arrays (not audio; this bypasses librosa/pyin entirely so the test is fast,
# deterministic, and exercises exactly the merge logic in isolation).
#
# Root cause this guards against: a real ~102s double-bass recording (see
# ARCHITECTURE.md risk area #1) showed a cluster of "final" (post-octave-
# -correction) detected notes off by 9-11 or 13-15 semitones from the
# reference - near a full octave (12) but not clean. Root-caused to
# `_split_oversized_segments`'s pitch-change sub-splitting: a slurred
# transition (finger sliding between positions, or a bow-noise transient at
# the join) produces a handful of consecutive frames whose smoothed pitch
# briefly reads as some other, unrelated note. With no minimum-run-length
# floor, that handful of frames became its OWN reported note - and because
# its (wrong) pitch estimate already fell inside `[min_midi, max_midi]`,
# `RangeClampOctaveCorrector` had nothing to correct (it only acts on
# out-of-range notes). ~73% (24/33) of the near-octave-error cluster in the
# full-etude validation traced to exactly this mechanism.
# --------------------------------------------------------------------------- #

def _synthetic_frames(midi_sequence: list[tuple[int, int]], hop_s: float = 0.01):
    """Build (times, f0, voiced_flag, voiced_prob) arrays from a list of
    (midi, frame_count) pairs at a constant hop, all voiced at fixed
    confidence. Lets tests target `_split_oversized_segments` directly
    without going through pyin/librosa at all.
    """
    freqs: list[float] = []
    for midi, count in midi_sequence:
        freqs.extend([_midi_to_hz(midi)] * count)
    n = len(freqs)
    times = np.arange(n) * hop_s
    f0 = np.array(freqs, dtype=np.float64)
    voiced_flag = np.ones(n, dtype=bool)
    voiced_prob = np.full(n, 0.8)
    return times, f0, voiced_flag, voiced_prob


def test_split_oversized_segments_merges_short_transition_fragment() -> None:
    # A slurred segment: a stable note (midi 40) for 20 frames, a brief
    # 2-frame transition blip landing on an unrelated pitch (midi 52 -
    # standing in for a real slide/bow-noise artifact), then a second
    # stable note (midi 44) for 20 frames. 2 frames is below
    # MIN_SPLIT_RUN_FRAMES, so the blip must be absorbed into a neighboring
    # run rather than reported as a third, spurious note.
    assert 2 < MIN_SPLIT_RUN_FRAMES
    hop_s = 0.01
    times, f0, voiced_flag, voiced_prob = _synthetic_frames(
        [(40, 20), (52, 2), (44, 20)], hop_s=hop_s
    )
    long_seg = _Segment(0.0, len(f0) * hop_s, 40, 0.0, 0.8)
    # Several short "normal" segments before it so the piece's median
    # segment duration is small enough that `long_seg` (0.42s) clearly
    # exceeds OVERSIZED_FACTOR (2.2x) of it and gets sub-split at all.
    normal_segs = [_Segment(-0.1 * (i + 1), -0.1 * i, 41, 0.0, 0.8) for i in range(1, 6)]

    result = _split_oversized_segments(normal_segs + [long_seg], times, f0, voiced_flag, voiced_prob)

    midis = [s.midi for s in result if s.onset_s >= 0.0]
    assert 52 not in midis, "short transition blip must not be reported as its own note"
    assert 40 in midis and 44 in midis, "both real notes on either side must survive"


def test_split_oversized_segments_keeps_run_at_exactly_the_minimum() -> None:
    # A run exactly MIN_SPLIT_RUN_FRAMES long is a real (if brief) note, not
    # a transition artifact, and must be kept as its own segment.
    hop_s = 0.01
    times, f0, voiced_flag, voiced_prob = _synthetic_frames(
        [(40, 20), (46, MIN_SPLIT_RUN_FRAMES), (44, 20)], hop_s=hop_s
    )
    long_seg = _Segment(0.0, len(f0) * hop_s, 40, 0.0, 0.8)
    normal_segs = [_Segment(-0.1 * (i + 1), -0.1 * i, 41, 0.0, 0.8) for i in range(1, 6)]

    result = _split_oversized_segments(normal_segs + [long_seg], times, f0, voiced_flag, voiced_prob)

    midis = [s.midi for s in result if s.onset_s >= 0.0]
    assert 40 in midis and 46 in midis and 44 in midis


def test_split_oversized_segments_recomputes_pitch_after_merge() -> None:
    # After a short fragment is merged into a neighbor, the merged
    # segment's reported pitch must be recomputed from the COMBINED frames
    # (via `_pitch_stats`), not left as the stale single-frame estimate
    # from before the merge - otherwise the merge silently keeps reporting
    # a bad boundary-frame pitch instead of the more stable neighbor pitch.
    hop_s = 0.01
    # note at midi 40 for 20 frames, then 1 stray frame that rounds to 41
    # (adjacent semitone) which should merge into the midi-40 run and not
    # change its reported pitch.
    times, f0, voiced_flag, voiced_prob = _synthetic_frames(
        [(40, 20), (41, 1)], hop_s=hop_s
    )
    long_seg = _Segment(0.0, len(f0) * hop_s, 40, 0.0, 0.8)
    normal_segs = [_Segment(-0.1 * (i + 1), -0.1 * i, 41, 0.0, 0.8) for i in range(1, 6)]

    result = _split_oversized_segments(normal_segs + [long_seg], times, f0, voiced_flag, voiced_prob)

    real_segments = [s for s in result if s.onset_s >= 0.0]
    assert len(real_segments) == 1
    assert real_segments[0].midi == 40
