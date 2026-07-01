"""Tests for `RuleBasedAssessor` (Phase 3 of the MVP plan: assessment).

All fixtures are synthetic Python objects built directly against the
pydantic contracts (Score/Transcription/Alignment) - no MusicXML or audio
needed, mirroring how `test_offline_dtw_aligner.py` tests alignment in
isolation.
"""

from __future__ import annotations

import pytest

from app.modules.assessment import MistakeType, OctavePolicy, Severity, ToleranceProfile, builtin_profiles
from app.modules.rule_based_assessor import RuleBasedAssessor
from app.modules.score_align import Alignment, AlignMode, AlignStrategy, NotePair
from app.modules.score_ingest import Score, ScoreNote, ScoreSourceFormat, TempoReference
from app.modules.transcription import DetectedNote, Transcription, TranscriptionConfig

BPM_60 = TempoReference(bpm=60.0)  # 1 beat == 1 second, convenient for tests


def _score(notes: list[ScoreNote], tempo: TempoReference = BPM_60) -> Score:
    return Score(
        score_id="test-score",
        source_format=ScoreSourceFormat.MUSICXML,
        tempo=tempo,
        notes=notes,
    )


def _note(index: int, midi: int, onset_beats: float, duration_beats: float = 1.0, tied_from_prev: bool = False) -> ScoreNote:
    return ScoreNote(
        index=index,
        midi=midi,
        pitch_class=midi % 12,
        onset_beats=onset_beats,
        duration_beats=duration_beats,
        tied_from_prev=tied_from_prev,
    )


def _performance(notes: list[DetectedNote]) -> Transcription:
    return Transcription(notes=notes, config=TranscriptionConfig(), sample_rate=44100, duration_s=10.0)


def _detected(index: int, midi: int, onset_s: float, cents_offset: float = 0.0, confidence: float = 0.9) -> DetectedNote:
    return DetectedNote(
        index=index, midi=midi, cents_offset=cents_offset,
        onset_s=onset_s, offset_s=onset_s + 0.9, confidence=confidence,
    )


def _alignment(pairs: list[NotePair]) -> Alignment:
    return Alignment(mode=AlignMode.OFFLINE, strategy=AlignStrategy.GLOBAL_DTW, pairs=pairs)


PROFILE = ToleranceProfile(
    name="test", pitch_tolerance_cents=50.0, timing_tolerance_ms=100.0,
    octave_policy=OctavePolicy.CORRECT_WITH_WARNING, min_confidence_for_pitch_error=0.5,
)


@pytest.fixture
def assessor() -> RuleBasedAssessor:
    return RuleBasedAssessor()


def test_perfect_performance_is_all_correct(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0), _note(1, 45, 1.0)])
    performance = _performance([_detected(0, 43, 0.0), _detected(1, 45, 1.0)])
    alignment = _alignment([NotePair(ref_index=0, performed_index=0), NotePair(ref_index=1, performed_index=1)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert result.mistakes == []
    assert result.correct_ref_indices == [0, 1]


def test_wrong_pitch_different_pitch_class(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])  # G2
    performance = _performance([_detected(0, 45, 0.0)])  # A2 - different pitch class
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    m = result.mistakes[0]
    assert m.type == MistakeType.WRONG_PITCH
    assert m.severity == Severity.ERROR
    assert m.cents_off == pytest.approx(200.0)
    assert result.correct_ref_indices == []


def test_small_pitch_deviation_within_tolerance_is_correct(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 43, 0.0, cents_offset=20.0)])  # within 50c tolerance
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert result.mistakes == []
    assert result.correct_ref_indices == [0]


def test_octave_off_correct_with_warning_default(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])  # G2
    performance = _performance([_detected(0, 55, 0.0)])  # G3, one octave high
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    m = result.mistakes[0]
    assert m.type == MistakeType.OCTAVE_OFF
    assert m.severity == Severity.INFO
    assert m.cents_off == pytest.approx(1200.0)
    # Octave-off under CORRECT_WITH_WARNING is flagged but NOT counted as a
    # hard error; per AssessmentResult.has_review_flags it should read True.
    assert result.has_review_flags
    assert result.correct_ref_indices == []  # info flag still recorded as a mistake, not "clean"


def test_octave_off_hard_error_policy_counts_as_wrong_pitch(assessor: RuleBasedAssessor) -> None:
    profile = PROFILE.model_copy(update={"octave_policy": OctavePolicy.HARD_ERROR})
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 55, 0.0)])
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, profile)

    assert len(result.mistakes) == 1
    assert result.mistakes[0].type == MistakeType.WRONG_PITCH
    assert result.mistakes[0].severity == Severity.ERROR


def test_octave_off_ignore_policy_is_fully_correct(assessor: RuleBasedAssessor) -> None:
    profile = PROFILE.model_copy(update={"octave_policy": OctavePolicy.IGNORE})
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 55, 0.0)])
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, profile)

    assert result.mistakes == []
    assert result.correct_ref_indices == [0]


def test_timing_late_beyond_tolerance(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 43, 0.3)])  # 300ms late, tolerance is 100ms
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    m = result.mistakes[0]
    assert m.type == MistakeType.TIMING_LATE
    assert m.timing_error_ms == pytest.approx(300.0)


def test_timing_early_beyond_tolerance(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 1.0)])  # onset at 1s (60bpm)
    performance = _performance([_detected(0, 43, 0.7)])  # 300ms early
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    assert result.mistakes[0].type == MistakeType.TIMING_EARLY


def test_wrong_pitch_and_late_both_recorded(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 45, 0.3)])  # wrong pitch class AND 300ms late
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    types = {m.type for m in result.mistakes}
    assert types == {MistakeType.WRONG_PITCH, MistakeType.TIMING_LATE}


def test_missed_note(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0), _note(1, 45, 1.0)])
    performance = _performance([_detected(0, 43, 0.0)])
    alignment = _alignment([
        NotePair(ref_index=0, performed_index=0),
        NotePair(ref_index=1, performed_index=None),
    ])

    result = assessor.assess(alignment, score, performance, PROFILE)

    missed = [m for m in result.mistakes if m.type == MistakeType.MISSED_NOTE]
    assert len(missed) == 1
    assert missed[0].ref_index == 1
    assert missed[0].performed_index is None
    assert missed[0].severity == Severity.ERROR


def test_tied_continuation_note_with_no_performed_match_is_not_a_miss(assessor: RuleBasedAssessor) -> None:
    # A tied note has no new attack - alignment legitimately produces no
    # performed match for its index. That must NOT be flagged as missed.
    score = _score([
        _note(0, 40, 0.0, duration_beats=2.0),
        _note(1, 40, 2.0, tied_from_prev=True),
        _note(2, 43, 3.0),
    ])
    performance = _performance([_detected(0, 40, 0.0), _detected(1, 43, 3.0)])
    alignment = _alignment([
        NotePair(ref_index=0, performed_index=0),
        NotePair(ref_index=1, performed_index=None),
        NotePair(ref_index=2, performed_index=1),
    ])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert not any(m.type == MistakeType.MISSED_NOTE for m in result.mistakes)
    assert result.correct_ref_indices == [0, 1, 2]


def test_extra_note(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 43, 0.0), _detected(1, 45, 1.0)])
    alignment = _alignment([
        NotePair(ref_index=0, performed_index=0),
        NotePair(ref_index=None, performed_index=1),
    ])

    result = assessor.assess(alignment, score, performance, PROFILE)

    extra = [m for m in result.mistakes if m.type == MistakeType.EXTRA_NOTE]
    assert len(extra) == 1
    assert extra[0].performed_index == 1
    assert extra[0].ref_index is None
    assert extra[0].severity == Severity.WARNING


def test_low_confidence_suppresses_wrong_pitch_verdict(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 45, 0.0, confidence=0.2)])  # below 0.5 threshold
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    m = result.mistakes[0]
    assert m.type == MistakeType.LOW_CONFIDENCE
    assert m.severity == Severity.INFO
    assert result.has_review_flags


def test_low_confidence_does_not_suppress_timing_mistakes(assessor: RuleBasedAssessor) -> None:
    score = _score([_note(0, 43, 0.0)])
    performance = _performance([_detected(0, 43, 0.5, confidence=0.1)])  # correct pitch, very late, low conf
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    assert result.mistakes[0].type == MistakeType.TIMING_LATE


def test_gradual_tempo_drift_does_not_generate_false_late_verdicts(assessor: RuleBasedAssessor) -> None:
    """The real-world bug this feature fixes: a performer who plays freely
    (not to a click track) and gradually slows down should NOT accumulate
    growing "late" mistakes just because their overall pace drifted from
    the printed tempo - as long as they're internally consistent with their
    OWN pace. Score is 5 quarter notes at 60bpm (1 beat = 1s); performance
    plays the same notes but increasingly slower (drifting up to 800ms
    "late" of the fixed-tempo expectation by note 4) - a real recording's
    kind of gradual rubato, not a click-track performance."""
    score = _score([_note(i, 40 + i, float(i)) for i in range(5)])
    # onsets: 0.0, 1.1, 2.3, 3.6, 5.0 - increasingly slower than 1s/beat,
    # but each individual gap is close to its neighbors' pace.
    onsets = [0.0, 1.1, 2.3, 3.6, 5.0]
    performance = _performance([_detected(i, 40 + i, onsets[i]) for i in range(5)])
    alignment = _alignment([NotePair(ref_index=i, performed_index=i) for i in range(5)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    timing_mistakes = [m for m in result.mistakes if m.type in (MistakeType.TIMING_LATE, MistakeType.TIMING_EARLY)]
    assert timing_mistakes == [], (
        f"expected no timing mistakes under gradual drift, got {timing_mistakes}"
    )


def test_note_genuinely_out_of_place_is_still_caught_despite_drift(assessor: RuleBasedAssessor) -> None:
    """Tempo-elasticity must not become "nothing is ever late". A note that's
    genuinely early relative to ITS NEIGHBORS - not just relative to a fixed
    bpm - should still be flagged, even inside an otherwise-drifting
    performance.

    Honest note on what this test does NOT claim: because the curve is
    built from immediate neighbors (see _TempoCurve's docstring), a single
    badly-mistimed note visibly distorts its two immediate neighbors' local
    slope estimates too - that's an inherent property of any simple local
    method, not a bug, and this test doesn't pretend otherwise. What it
    does assert: the genuinely mistimed note is caught, its deviation is
    unambiguously the largest, and the distortion does NOT cascade past its
    immediate neighbors to the rest of an otherwise well-behaved take.
    """
    score = _score([_note(i, 40 + i, float(i)) for i in range(7)])
    # Notes 0,1,2,4,5,6 follow a smoothly-drifting pace (increasing gaps,
    # same "free performance" pattern as the drift test above). Note 3 is
    # genuinely rushed - played ~900ms earlier than that smooth pace would
    # predict - well clear of either boundary so this isn't an
    # extrapolation edge case.
    onsets = [0.0, 1.03, 2.12, 2.5, 4.65, 6.05, 7.6]
    performance = _performance([_detected(i, 40 + i, onsets[i]) for i in range(7)])
    alignment = _alignment([NotePair(ref_index=i, performed_index=i) for i in range(7)])
    # Wider tolerance than PROFILE's 100ms: the point of this test is the
    # anomaly vs. its neighbors' relative sizes, not tuning exact numbers
    # to dodge float-boundary coincidences in the "normal drift" residual.
    profile = PROFILE.model_copy(update={"timing_tolerance_ms": 200.0})

    result = assessor.assess(alignment, score, performance, profile)

    timing_mistakes = [m for m in result.mistakes if m.type in (MistakeType.TIMING_LATE, MistakeType.TIMING_EARLY)]
    assert timing_mistakes, "the genuinely mistimed note should be caught"
    by_ref = {m.ref_index: m for m in timing_mistakes}
    assert 3 in by_ref
    assert by_ref[3].type == MistakeType.TIMING_EARLY
    largest = max(timing_mistakes, key=lambda m: abs(m.timing_error_ms))
    assert largest.ref_index == 3, "the true anomaly should dominate, not a neighbor"
    # Containment: notes 0, 1, 5, 6 are far enough from the anomaly that
    # they must stay clean - only its immediate neighbors (2, 4) may show
    # secondary distortion.
    assert {m.ref_index for m in timing_mistakes} <= {2, 3, 4}


def test_single_matched_note_falls_back_to_fixed_tempo(assessor: RuleBasedAssessor) -> None:
    """With fewer than 2 OTHER matched anchors there's nothing to
    interpolate from, so timing falls back to score.tempo.bpm exactly as
    before this feature existed - this is what keeps every pre-existing
    single/near-empty-alignment test in this file passing unchanged."""
    score = _score([_note(0, 43, 2.0)])  # onset at 2s under 60bpm
    performance = _performance([_detected(0, 43, 2.5)])  # 500ms late
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    result = assessor.assess(alignment, score, performance, PROFILE)

    assert len(result.mistakes) == 1
    assert result.mistakes[0].type == MistakeType.TIMING_LATE
    assert result.mistakes[0].timing_error_ms == pytest.approx(500.0)


def test_beginner_profile_is_more_forgiving_than_advanced() -> None:
    assessor = RuleBasedAssessor()
    profiles = builtin_profiles()
    score = _score([_note(0, 43, 0.0)])
    # 40 cents sharp, 100ms late: within beginner tolerance, outside advanced.
    performance = _performance([_detected(0, 43, 0.1, cents_offset=40.0)])
    alignment = _alignment([NotePair(ref_index=0, performed_index=0)])

    beginner_result = assessor.assess(alignment, score, performance, profiles["beginner"])
    advanced_result = assessor.assess(alignment, score, performance, profiles["advanced"])

    assert beginner_result.mistakes == []
    assert beginner_result.correct_ref_indices == [0]
    assert len(advanced_result.mistakes) >= 1  # too sharp and/or too late for advanced
