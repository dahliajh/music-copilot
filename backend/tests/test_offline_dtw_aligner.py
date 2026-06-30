"""Tests for the v1 OfflineDtwAligner (Phase 3 of the MVP plan).

Fixtures are built directly in Python — no MusicXML, no real audio. We construct
synthetic `Score`/`ScoreNote`/`TempoReference` and `Transcription`/`DetectedNote`
objects so each test exercises exactly one alignment behaviour.

Important boundary (per docs/ARCHITECTURE.md): the aligner produces an
*alignment*, not an error classification. A wrong note still *aligns* 1:1 (with
elevated local_cost); deciding it is "wrong" is the assessment module's job.
"""

from __future__ import annotations

import pytest

from app.modules.offline_dtw_aligner import OfflineDtwAligner
from app.modules.score_align import (
    AlignConfig,
    AlignMode,
    AlignStrategy,
)
from app.modules.score_ingest import (
    Score,
    ScoreNote,
    ScoreSourceFormat,
    TempoReference,
)
from app.modules.transcription import (
    DetectedNote,
    PitchAlgorithm,
    Transcription,
    TranscriptionConfig,
)

BPM = 120.0
SEC_PER_BEAT = 60.0 / BPM  # 0.5s at 120 bpm


# --------------------------------------------------------------------- helpers


def make_score(midis: list[int], *, bpm: float = BPM) -> Score:
    """Build a Score: one quarter-note per midi, one beat apart."""
    notes = [
        ScoreNote(
            index=i,
            midi=m,
            pitch_class=m % 12,
            onset_beats=float(i),
            duration_beats=1.0,
        )
        for i, m in enumerate(midis)
    ]
    return Score(
        score_id="test",
        title="synthetic",
        source_format=ScoreSourceFormat.MUSICXML,
        tempo=TempoReference(bpm=bpm, beats_per_measure=4),
        notes=notes,
    )


def make_transcription(
    events: list[tuple[int, float]], *, confidence: float = 0.9
) -> Transcription:
    """Build a Transcription from (midi, onset_seconds) tuples."""
    notes = [
        DetectedNote(
            index=i,
            midi=m,
            cents_offset=0.0,
            onset_s=onset,
            offset_s=onset + SEC_PER_BEAT * 0.9,
            confidence=confidence,
        )
        for i, (m, onset) in enumerate(events)
    ]
    return Transcription(
        notes=notes,
        config=TranscriptionConfig(algorithm=PitchAlgorithm.PYIN),
        sample_rate=44100,
        duration_s=(notes[-1].offset_s if notes else 0.0),
    )


def perf_from_score(midis: list[int]) -> Transcription:
    """A performance that plays the given midis exactly on the beat grid."""
    return make_transcription([(m, i * SEC_PER_BEAT) for i, m in enumerate(midis)])


@pytest.fixture
def aligner() -> OfflineDtwAligner:
    return OfflineDtwAligner()


# ------------------------------------------------------------------ mode tests


def test_supports_mode_offline_only(aligner: OfflineDtwAligner) -> None:
    assert aligner.supports_mode(AlignMode.OFFLINE) is True
    assert aligner.supports_mode(AlignMode.ONLINE) is False


def test_align_incremental_raises(aligner: OfflineDtwAligner) -> None:
    perf = perf_from_score([60])
    score = make_score([60])
    with pytest.raises(NotImplementedError):
        aligner.align_incremental(perf, score)


# ----------------------------------------------------------- perfect match


def test_perfect_match_global(aligner: OfflineDtwAligner) -> None:
    midis = [60, 62, 64, 65, 67]
    score = make_score(midis)
    perf = perf_from_score(midis)

    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.GLOBAL_DTW)
    )

    assert align.mode == AlignMode.OFFLINE
    assert align.is_partial is False
    # Every pair is a real 1:1 match.
    assert len(align.pairs) == len(midis)
    for k, p in enumerate(align.pairs):
        assert p.ref_index == k
        assert p.performed_index == k
        assert p.local_cost == pytest.approx(0.0, abs=1e-9)
    # Zero gaps, zero skip/repeat spans.
    assert all(
        p.ref_index is not None and p.performed_index is not None
        for p in align.pairs
    )
    assert align.skip_repeat_spans == []


def test_perfect_match_subsequence(aligner: OfflineDtwAligner) -> None:
    midis = [60, 62, 64, 65, 67]
    score = make_score(midis)
    perf = perf_from_score(midis)
    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.SUBSEQUENCE_DTW)
    )
    matched = [p for p in align.pairs if p.performed_index is not None]
    assert len(matched) == len(midis)
    for p in matched:
        assert p.ref_index == p.performed_index
        assert p.local_cost == pytest.approx(0.0, abs=1e-9)


# ----------------------------------------------------------- wrong note


def test_one_wrong_note_still_aligns_1to1(aligner: OfflineDtwAligner) -> None:
    """A pitch substitution must still align 1:1 (the aligner does not classify
    severity), but the substituted pair carries elevated local_cost."""
    score_midis = [60, 62, 64, 65, 67]
    perf_midis = [60, 62, 67, 65, 67]  # 3rd note 64 -> 67 (a wrong neighbour,
    #                                     not an octave); a plausible misfinger
    #                                     that stays under the gap threshold so
    #                                     the pair still aligns 1:1.
    score = make_score(score_midis)
    perf = perf_from_score(perf_midis)

    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.GLOBAL_DTW)
    )

    # Still a full 1:1 path, no gaps.
    assert len(align.pairs) == 5
    assert all(
        p.ref_index is not None and p.performed_index is not None
        for p in align.pairs
    )
    by_ref = {p.ref_index: p for p in align.pairs}
    assert by_ref[2].performed_index == 2
    # The wrong-note pair costs more than its correct neighbours.
    assert by_ref[2].local_cost > by_ref[1].local_cost
    assert by_ref[2].local_cost > 0.0


def test_octave_error_still_aligns(aligner: OfflineDtwAligner) -> None:
    """An octave error must still ALIGN (cheap pitch cost), not gap out — octave
    correction is downstream. It should cost less than an unrelated wrong note."""
    score_midis = [60, 62, 64]
    octave_perf = perf_from_score([60, 50, 64])  # 62 -> 50 (octave + a bit) ...
    # Use a clean octave: 62 -> 74 (exactly one octave up).
    octave_perf = perf_from_score([60, 74, 64])
    unrelated_perf = perf_from_score([60, 69, 64])  # 62 -> 69 unrelated

    score = make_score(score_midis)
    cfg = AlignConfig(strategy=AlignStrategy.GLOBAL_DTW)

    oct_align = aligner.align(octave_perf, score, cfg)
    unr_align = aligner.align(unrelated_perf, score, cfg)

    # Both still align 1:1 (no gaps).
    assert all(p.performed_index is not None for p in oct_align.pairs)
    oct_cost = {p.ref_index: p.local_cost for p in oct_align.pairs}[1]
    unr_cost = {p.ref_index: p.local_cost for p in unr_align.pairs}[1]
    # Octave error is cheaper than an unrelated wrong note.
    assert oct_cost < unr_cost


# ----------------------------------------------------------- missed note


def test_one_missed_note(aligner: OfflineDtwAligner) -> None:
    """Score has a note the performer never played -> a NotePair with
    performed_index=None at the right ref_index."""
    score_midis = [60, 62, 64, 65, 67]
    # Performer omits the 3rd score note (index 2, midi 64). Remaining notes keep
    # their original on-the-beat timing so they still match their score onsets.
    perf = make_transcription(
        [
            (60, 0 * SEC_PER_BEAT),
            (62, 1 * SEC_PER_BEAT),
            (65, 3 * SEC_PER_BEAT),
            (67, 4 * SEC_PER_BEAT),
        ]
    )
    score = make_score(score_midis)

    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.GLOBAL_DTW)
    )

    missed = [
        p for p in align.pairs if p.performed_index is None and p.ref_index is not None
    ]
    assert len(missed) == 1
    assert missed[0].ref_index == 2  # the omitted score note
    # The four played notes all match.
    matched = [p for p in align.pairs if p.performed_index is not None]
    assert len(matched) == 4


# ----------------------------------------------------------- extra note


def test_one_extra_note(aligner: OfflineDtwAligner) -> None:
    """Performer inserts a note with no score counterpart -> NotePair with
    ref_index=None."""
    score_midis = [60, 62, 64]
    # Insert a stray note (midi 55) between score notes 1 and 2, off the grid.
    perf = make_transcription(
        [
            (60, 0 * SEC_PER_BEAT),
            (62, 1 * SEC_PER_BEAT),
            (55, 1.5 * SEC_PER_BEAT),  # extra
            (64, 2 * SEC_PER_BEAT),
        ]
    )
    score = make_score(score_midis)

    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.GLOBAL_DTW)
    )

    extra = [
        p for p in align.pairs if p.ref_index is None and p.performed_index is not None
    ]
    assert len(extra) == 1
    assert extra[0].performed_index == 2  # the inserted note
    # All three score notes are still matched.
    matched_refs = sorted(
        p.ref_index for p in align.pairs if p.ref_index is not None
        and p.performed_index is not None
    )
    assert matched_refs == [0, 1, 2]


def test_gap_threshold_respected_not_forced(aligner: OfflineDtwAligner) -> None:
    """A note far from anything in the score must not be force-matched: with a
    tight gap threshold it becomes a one-sided pair instead."""
    score_midis = [60, 62, 64]
    perf = make_transcription(
        [
            (60, 0 * SEC_PER_BEAT),
            (62, 1 * SEC_PER_BEAT),
            (55, 1.5 * SEC_PER_BEAT),  # nothing close in the score
            (64, 2 * SEC_PER_BEAT),
        ]
    )
    score = make_score(score_midis)
    # gap_cost_threshold small enough that midi 55 can't masquerade as a match.
    cfg = AlignConfig(strategy=AlignStrategy.GLOBAL_DTW, gap_cost_threshold=2.0)
    align = aligner.align(perf, score, cfg)
    # The stray note is left unmatched.
    assert any(p.ref_index is None and p.performed_index == 2 for p in align.pairs)
    # No matched pair exceeds the threshold.
    for p in align.pairs:
        if p.ref_index is not None and p.performed_index is not None:
            assert p.local_cost <= cfg.gap_cost_threshold


# ----------------------------------------------------------- skipped section


def test_skipped_section_resync(aligner: OfflineDtwAligner) -> None:
    """Performer skips a whole middle section of the score. RESYNC must surface a
    'skipped' span and NOT force a bad monotonic match through the gap."""
    # 10-note score; performer plays notes 0,1 then jumps to 7,8,9 (skips 2..6).
    score_midis = [60, 62, 64, 65, 67, 69, 71, 72, 74, 76]
    score = make_score(score_midis)

    # Performer plays the first two notes, omits the middle, and resumes at score
    # notes 7,8,9 at THEIR true score onset times (beats 7,8,9). A real skip keeps
    # the score's clock — the player just leaves notes out — so the resumed notes
    # land at their notated times, and the only cheap alignment deletes the
    # un-played interior rather than force-matching the high notes onto low ones.
    played = [
        (60, 0 * SEC_PER_BEAT),
        (62, 1 * SEC_PER_BEAT),
        (72, 7 * SEC_PER_BEAT),  # score note 7
        (74, 8 * SEC_PER_BEAT),  # score note 8
        (76, 9 * SEC_PER_BEAT),  # score note 9
    ]
    perf = make_transcription(played)

    cfg = AlignConfig(strategy=AlignStrategy.RESYNC, resync_window=2)
    align = aligner.align(perf, score, cfg)

    assert align.strategy == AlignStrategy.RESYNC
    # A skipped span must be reported.
    skipped = [s for s in align.skip_repeat_spans if s.kind == "skipped"]
    assert skipped, "expected a 'skipped' skip_repeat_span"
    span = skipped[0]
    # The skipped span covers (a subset of) the middle score notes 2..6.
    assert 2 <= span.ref_start
    assert span.ref_end <= 6

    # The aligner must not force the played high notes onto the wrong (low) score
    # notes: the performed midi-72/74/76 notes should map to score indices 7/8/9
    # (their true home), not be smeared across the skipped region.
    matched = {
        p.performed_index: p.ref_index
        for p in align.pairs
        if p.ref_index is not None and p.performed_index is not None
    }
    # performed notes 2,3,4 are the 72/74/76 — they should land in the 7..9 range.
    for perf_idx in (2, 3, 4):
        assert matched.get(perf_idx) is not None
        assert matched[perf_idx] >= 7

    # Two monotonic segments (before/after the skip), re-anchored.
    assert len(align.segments) == 2


# ----------------------------------------------------------- repeated section


def test_repeated_section_resync(aligner: OfflineDtwAligner) -> None:
    """Performer repeats a span (plays part of it twice).

    NOTE on what's actually verified: skip detection is solid; full *repeat*
    detection (proving the replayed notes re-cover a specific earlier score span)
    is heuristic — see the aligner's `_classify_span` docstring and ARCHITECTURE.
    Here we verify the weaker, honest guarantee: the aligner does not crash, the
    legitimate score notes still align, and the surplus replayed notes are
    surfaced as a structured break (a 'repeated' span and/or extra one-sided
    pairs) rather than being force-matched onto unrelated score notes.
    """
    score_midis = [60, 62, 64, 65, 67]
    score = make_score(score_midis)

    # Performer plays 0,1,2 then repeats 1,2 again, then continues 3,4.
    played = [
        (60, 0.0 * SEC_PER_BEAT),  # score 0
        (62, 1.0 * SEC_PER_BEAT),  # score 1
        (64, 2.0 * SEC_PER_BEAT),  # score 2
        (62, 3.0 * SEC_PER_BEAT),  # REPEAT of score 1
        (64, 4.0 * SEC_PER_BEAT),  # REPEAT of score 2
        (65, 5.0 * SEC_PER_BEAT),  # score 3
        (67, 6.0 * SEC_PER_BEAT),  # score 4
    ]
    perf = make_transcription(played)

    cfg = AlignConfig(strategy=AlignStrategy.RESYNC, resync_window=1)
    align = aligner.align(perf, score, cfg)

    assert align.strategy == AlignStrategy.RESYNC
    # All five legitimate score notes get covered by at least one performed note.
    matched_refs = {
        p.ref_index
        for p in align.pairs
        if p.ref_index is not None and p.performed_index is not None
    }
    assert {0, 1, 2, 3, 4}.issubset(matched_refs)

    # The replayed surplus is surfaced structurally: either an explicit
    # 'repeated' span, or extra (ref_index=None) performed pairs — not silently
    # force-matched onto unrelated notes.
    has_repeat_span = any(s.kind == "repeated" for s in align.skip_repeat_spans)
    has_extra_pairs = any(
        p.ref_index is None and p.performed_index is not None for p in align.pairs
    )
    assert has_repeat_span or has_extra_pairs

    # Sanity: every performed note appears exactly once in the flattened path.
    perf_indices = sorted(
        p.performed_index for p in align.pairs if p.performed_index is not None
    )
    assert perf_indices == list(range(len(played)))


# ----------------------------------------------------------- subsequence span


def test_subsequence_matches_subspan(aligner: OfflineDtwAligner) -> None:
    """Performance covers only a contiguous middle sub-span of the score; the
    un-played prefix/suffix must be free (not penalised as a huge gap run)."""
    score_midis = [60, 62, 64, 65, 67, 69, 71]
    score = make_score(score_midis)
    # Performer plays only score notes 2,3,4 (midis 64,65,67), at their true
    # score onset times.
    perf = make_transcription(
        [
            (64, 2 * SEC_PER_BEAT),
            (65, 3 * SEC_PER_BEAT),
            (67, 4 * SEC_PER_BEAT),
        ]
    )
    align = aligner.align(
        perf, score, AlignConfig(strategy=AlignStrategy.SUBSEQUENCE_DTW)
    )
    matched = {
        p.performed_index: p.ref_index
        for p in align.pairs
        if p.performed_index is not None and p.ref_index is not None
    }
    assert matched == {0: 2, 1: 3, 2: 4}
    # Performed notes are all matched (no spurious extra-note gaps).
    assert all(p.performed_index is None or p.ref_index is not None for p in align.pairs)
