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
    PyinPitchTracker,
    PyinTranscriber,
    RangeClampOctaveCorrector,
    _frame_length_for,
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
