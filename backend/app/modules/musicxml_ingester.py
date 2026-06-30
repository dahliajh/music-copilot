"""MusicXML score ingestion — v1 concrete `ScoreIngester` implementation.

Uses music21 to parse a MusicXML file into the canonical `Score` model
defined in `score_ingest.py`. Scope (per the MVP plan, Phase 1): clean,
already-valid MusicXML for a single monophonic part — hand-entered, or a
typeset PDF someone converted to MusicXML by hand. Optical Music
Recognition (phone photo -> MusicXML) is the v1.5/v2 path and out of scope
here; this only consumes MusicXML that already exists.

Design notes:
  * Rests are not represented in `Score.notes` (there's no pitch to assign)
    and are skipped when assigning `ScoreNote.index` — the index counts
    only actual notes, matching the frontend's OSMD note-walk (see
    `ScoreView.tsx`) and the `ref_index` convention used throughout
    alignment/assessment. `onset_beats` still comes from music21's
    absolute offset, so gaps from skipped rests don't shift later notes.
  * Ties are preserved as separate `ScoreNote` entries (not merged into one
    longer note) with `tied_from_prev=True` on the continuation note, so
    downstream onset detection knows not to expect a new attack there.
  * Anything that violates the "single monophonic part, single voice"
    assumption (multiple parts, multiple voices, chords) is handled by
    best-effort degradation — pick the first part, the lowest-numbered
    voice, the lowest pitch of a chord — and surfaced as a
    `ScoreIngestError` warning rather than failing outright, per the
    contract's design intent for recoverable/lossy parses.
  * A source with no explicit tempo marking gets a default BPM (flagged as
    a warning), since alignment/assessment need *some* tempo reference —
    see the plan's "click track / tempo reference" note under module 4.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from music21 import converter
from music21 import tempo as m21_tempo

from .score_ingest import (
    Score,
    ScoreIngester,
    ScoreIngestError,
    ScoreIngestResult,
    ScoreNote,
    ScoreSourceFormat,
    TempoReference,
)

# Used only when the source has no explicit tempo marking. Arbitrary but
# documented; real tolerance/timing work should set an explicit tempo per
# the plan's "click track / tempo reference" note rather than rely on this.
DEFAULT_BPM = 60.0


class MusicXMLIngester(ScoreIngester):
    """v1 ScoreIngester: parses clean MusicXML via music21.

    Handles `ScoreSourceFormat.MUSICXML` and `MUSICXML_PDF` identically —
    "PDF" in that source format describes a human conversion step that
    already happened before the bytes reach this class, not a different
    parser path. `OMR_PHOTO` is v1.5+ and not implemented here.
    """

    def supports(self, source_format: ScoreSourceFormat) -> bool:
        return source_format in (
            ScoreSourceFormat.MUSICXML,
            ScoreSourceFormat.MUSICXML_PDF,
        )

    def ingest(
        self,
        raw: bytes,
        source_format: ScoreSourceFormat,
        *,
        score_id: Optional[str] = None,
    ) -> ScoreIngestResult:
        if not self.supports(source_format):
            raise ValueError(
                f"MusicXMLIngester does not support source_format={source_format!r}"
            )

        try:
            # Pass format= explicitly: music21's format auto-detection is
            # geared toward file paths/extensions, and silently mis-detects
            # raw bytes/byte-streams as other formats (observed: MuseData)
            # instead of raising, leading to a confusing unrelated traceback.
            parsed = converter.parse(raw, format="musicxml")
        except Exception as exc:  # music21 raises several distinct error types
            raise ValueError(f"Could not parse MusicXML: {exc}") from exc

        warnings: list[ScoreIngestError] = []

        part, part_warning = _select_part(parsed)
        if part_warning:
            warnings.append(part_warning)

        part, voice_warning = _select_voice(part)
        if voice_warning:
            warnings.append(voice_warning)

        tempo_ref, tempo_warning = _extract_tempo(parsed)
        if tempo_warning:
            warnings.append(tempo_warning)

        notes, note_warnings = _extract_notes(part)
        warnings.extend(note_warnings)

        score = Score(
            score_id=score_id or _derive_score_id(parsed, raw),
            title=_extract_title(parsed),
            source_format=source_format,
            tempo=tempo_ref,
            notes=notes,
        )
        return ScoreIngestResult(score=score, warnings=warnings)


def _select_part(parsed):
    """Pick the part to ingest, warning if there was more than one to choose from."""
    parts = list(parsed.parts)
    if not parts:
        raise ValueError("MusicXML has no parts to ingest.")
    if len(parts) > 1:
        names = ", ".join(p.partName or "?" for p in parts)
        return parts[0], ScoreIngestError(
            code="multiple_parts",
            message=(
                f"Score has {len(parts)} parts ({names}); v1 expects a single "
                "monophonic part. Using the first part and ignoring the rest."
            ),
        )
    return parts[0], None


def _select_voice(part):
    """Pick a single voice if the part has divisi/multiple voices.

    Single-voice content (the normal case) never produces `Voice` stream
    objects in music21 — they only appear when a measure actually has more
    than one simultaneous voice (e.g. a `<backup>` in the MusicXML). So an
    empty result here is the common path, not a missed case.
    """
    voice_ids = sorted({v.id for v in part.recurse().getElementsByClass("Voice")})
    if len(voice_ids) <= 1:
        return part, None

    split = part.voicesToParts()
    chosen = split.parts[0]
    return chosen, ScoreIngestError(
        code="multiple_voices",
        message=(
            f"Part has {len(voice_ids)} voices ({', '.join(voice_ids)}); v1 "
            "expects a single monophonic voice. Using the lowest-numbered "
            "voice and ignoring the rest."
        ),
    )


def _extract_tempo(parsed) -> tuple[TempoReference, Optional[ScoreIngestError]]:
    explicit = list(parsed.recurse().getElementsByClass(m21_tempo.TempoIndication))
    for mark in explicit:
        bpm = mark.getQuarterBPM()
        if bpm:
            return TempoReference(bpm=float(bpm)), None

    return TempoReference(bpm=DEFAULT_BPM), ScoreIngestError(
        code="missing_tempo",
        message=(
            f"No explicit tempo marking found in the source; defaulting to "
            f"{DEFAULT_BPM} BPM. Timing assessment will be unreliable until a "
            "real tempo/click reference is set for this score."
        ),
    )


def _extract_notes(part) -> tuple[list[ScoreNote], list[ScoreIngestError]]:
    elements = list(part.flatten().notesAndRests)

    notes: list[ScoreNote] = []
    warnings: list[ScoreIngestError] = []
    chord_count = 0
    index = 0

    for element in elements:
        if element.isRest:
            continue

        if element.isChord:
            chord_count += 1
            pitch = min(element.pitches, key=lambda p: p.midi)
        else:
            pitch = element.pitch

        tie = getattr(element, "tie", None)
        tied_from_prev = tie is not None and tie.type in ("stop", "continue")

        notes.append(
            ScoreNote(
                index=index,
                midi=int(pitch.midi),
                pitch_class=int(pitch.midi) % 12,
                onset_beats=float(element.offset),
                duration_beats=float(element.quarterLength),
                tied_from_prev=tied_from_prev,
            )
        )
        index += 1

    if chord_count:
        warnings.append(
            ScoreIngestError(
                code="chord_in_monophonic_part",
                message=(
                    f"{chord_count} chord(s) found in a part expected to be "
                    "monophonic; used the lowest pitch of each chord and "
                    "discarded the rest."
                ),
            )
        )

    if not notes:
        warnings.append(
            ScoreIngestError(
                code="no_notes",
                message="No notes found in the part (only rests, or an empty part).",
            )
        )

    return notes, warnings


def _extract_title(parsed) -> Optional[str]:
    md = parsed.metadata
    if md and md.title:
        return md.title
    return None


def _derive_score_id(parsed, raw: bytes) -> str:
    """Stable, content-derived id used when the caller doesn't supply one."""
    digest = hashlib.sha1(raw).hexdigest()[:12]
    title = _extract_title(parsed) or "score"
    slug = "-".join(filter(None, "".join(c.lower() if c.isalnum() else "-" for c in title).split("-")))
    return f"{slug}-{digest}" if slug else digest
