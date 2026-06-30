"""Tests for the v1 MusicXML ScoreIngester (Phase 1 of the MVP plan).

Fixtures live in backend/tests/fixtures/. `sample_bass_excerpt.musicxml` is
the same 10-note excerpt the frontend uses for its OSMD spike (copied here
so backend tests don't depend on the frontend's asset layout); most of the
rest are small synthetic files built to exercise one edge case each.

`bach_bwv140_7_bass_voice.musicxml` is the one *real* fixture: the bass
voice of J.S. Bach's chorale "Wachet auf, ruft uns die Stimme" (BWV 140,
No. 7), extracted from music21's bundled public-domain chorale corpus and
exported fresh via music21 - real engraving, not hand-typed by us. It's a
chorale bass *vocal* line rather than a part written for double bass, but
it's genuinely monophonic, bass-clef, public domain, and exercises real
ties/accidentals - and it's how a real round-trip bug got caught (see
test_real_world_title_extraction_fallback below). Real double-bass
method-book excerpts from actual school sheet music are still the better
long-term validation per the MVP plan (see STATUS.md) - this fixture is a
stand-in, not a replacement for that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.musicxml_ingester import MusicXMLIngester
from app.modules.score_ingest import ScoreSourceFormat

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def ingester() -> MusicXMLIngester:
    return MusicXMLIngester()


def test_supports_musicxml_and_pdf_not_omr(ingester: MusicXMLIngester) -> None:
    assert ingester.supports(ScoreSourceFormat.MUSICXML)
    assert ingester.supports(ScoreSourceFormat.MUSICXML_PDF)
    assert not ingester.supports(ScoreSourceFormat.OMR_PHOTO)


def test_ingest_rejects_unsupported_format(ingester: MusicXMLIngester) -> None:
    with pytest.raises(ValueError, match="does not support"):
        ingester.ingest(_read("sample_bass_excerpt.musicxml"), ScoreSourceFormat.OMR_PHOTO)


def test_ingest_rejects_unparseable_bytes(ingester: MusicXMLIngester) -> None:
    with pytest.raises(ValueError, match="Could not parse"):
        ingester.ingest(b"this is not MusicXML", ScoreSourceFormat.MUSICXML)


def test_happy_path_sample_excerpt(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(
        _read("sample_bass_excerpt.musicxml"),
        ScoreSourceFormat.MUSICXML,
        score_id="sample-bass-excerpt",
    )
    score = result.score

    assert score.score_id == "sample-bass-excerpt"
    assert score.title == "Phase 0 Spike — Sample Double Bass Excerpt"
    assert score.source_format == ScoreSourceFormat.MUSICXML
    assert not score.needs_manual_correction

    # Matches the inline "note index N" comments in the fixture file.
    expected_midi = [40, 43, 45, 47, 48, 47, 45, 43, 41, 40]  # E2 G2 A2 B2 C3 B2 A2 G2 F2 E2
    assert [n.midi for n in score.notes] == expected_midi
    assert [n.index for n in score.notes] == list(range(10))
    assert all(not n.tied_from_prev for n in score.notes)

    # No explicit tempo in this fixture -> should warn and use the default.
    assert score.tempo.bpm == 60.0
    assert any(w.code == "missing_tempo" for w in result.warnings)


def test_explicit_tempo_is_used_without_warning(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(_read("tie_and_rest.musicxml"), ScoreSourceFormat.MUSICXML)
    assert result.score.tempo.bpm == 72.0
    assert not any(w.code == "missing_tempo" for w in result.warnings)


def test_tie_preserved_as_separate_note_and_rest_skipped(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(_read("tie_and_rest.musicxml"), ScoreSourceFormat.MUSICXML)
    notes = result.score.notes

    # Fixture: E2 (tie start), E2 (tie stop), rest, G2 — rest must not appear,
    # and must not break index continuity or onset timing for the note after it.
    assert [n.midi for n in notes] == [40, 40, 43]
    assert [n.index for n in notes] == [0, 1, 2]
    assert notes[0].tied_from_prev is False
    assert notes[1].tied_from_prev is True
    assert notes[2].onset_beats == 3.0  # 2 beats tied note + 1 beat rest


def test_chord_in_monophonic_part_uses_lowest_pitch_and_warns(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(
        _read("chord_in_monophonic_part.musicxml"), ScoreSourceFormat.MUSICXML
    )
    notes = result.score.notes

    assert len(notes) == 1
    assert notes[0].midi == 36  # C2, the lower of the C2/E2 chord
    assert any(w.code == "chord_in_monophonic_part" for w in result.warnings)


def test_multiple_voices_picks_first_voice_and_warns(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(_read("multiple_voices.musicxml"), ScoreSourceFormat.MUSICXML)
    notes = result.score.notes

    # Fixture: voice 1 has a single half-note C2; voice 2 has two quarter G1s.
    # v1 should keep only voice 1.
    assert len(notes) == 1
    assert notes[0].midi == 36  # C2
    assert notes[0].duration_beats == 2.0
    assert any(w.code == "multiple_voices" for w in result.warnings)


def test_multiple_parts_uses_first_part_and_warns(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(_read("multiple_parts.musicxml"), ScoreSourceFormat.MUSICXML)
    notes = result.score.notes

    # Fixture: part 1 (Double Bass) has E2, G2; part 2 (Piano) has a whole-note C4.
    assert [n.midi for n in notes] == [40, 43]
    assert any(w.code == "multiple_parts" for w in result.warnings)


def test_caller_supplied_score_id_is_used_when_given(ingester: MusicXMLIngester) -> None:
    result = ingester.ingest(
        _read("sample_bass_excerpt.musicxml"), ScoreSourceFormat.MUSICXML, score_id="custom-id"
    )
    assert result.score.score_id == "custom-id"


def test_score_id_is_derived_and_stable_when_not_given(ingester: MusicXMLIngester) -> None:
    raw = _read("sample_bass_excerpt.musicxml")
    first = ingester.ingest(raw, ScoreSourceFormat.MUSICXML)
    second = ingester.ingest(raw, ScoreSourceFormat.MUSICXML)
    assert first.score.score_id == second.score.score_id
    assert first.score.score_id  # non-empty


def test_real_world_bach_chorale_bass_voice(ingester: MusicXMLIngester) -> None:
    """First real (non-hand-built) fixture: a genuine Bach chorale bass line.

    Doubles as a regression test for a real bug this fixture caught: the
    file only sets <movement-title>, not <work-title>, and `_extract_title`
    originally only checked the latter (via music21's `.title`), silently
    returning None. Fixed with `.bestTitle`.
    """
    result = ingester.ingest(
        _read("bach_bwv140_7_bass_voice.musicxml"), ScoreSourceFormat.MUSICXML
    )
    score = result.score

    assert score.title == "Wachet auf, ruft uns die Stimme — Bass voice (BWV 140, No. 7)"
    assert len(score.notes) == 82
    assert any(n.tied_from_prev for n in score.notes)  # real chorale ties across the bar
    # Sanity check we're not accidentally picking up a different voice: an
    # SATB bass line's range, including the occasional voice-crossing note
    # above middle C, not the soprano/alto/tenor ranges above it.
    assert min(n.midi for n in score.notes) < 48  # dips below C3
    assert max(n.midi for n in score.notes) < 67  # stays well under soprano range
    assert any(w.code == "missing_tempo" for w in result.warnings)  # chorale exports have none
    assert not any(w.code in ("chord_in_monophonic_part", "multiple_voices") for w in result.warnings)
