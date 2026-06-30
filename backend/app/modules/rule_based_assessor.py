"""Concrete v1 `Assessor`: classifies an `Alignment` into a structured
`Mistake` list using a `ToleranceProfile`.

Design notes (see docs/ARCHITECTURE.md, assessment.py):
  * Indices in `NotePair` are matched against `ScoreNote.index` /
    `DetectedNote.index` (stable identity fields), not raw list position.
    The v1 musicxml_ingester happens to keep these equal to list position,
    but this assessor doesn't assume that — it builds id->note lookup dicts
    so it stays correct even if that ever changes (e.g. after a manual
    score correction re-orders notes).
  * Octave-off classification follows `profile.octave_policy` exactly as
    specified in the architecture doc: same pitch-class + different MIDI
    octave is judged *after* the plain pitch-tolerance check fails, so a
    performance that's merely sharp/flat by a few cents is never
    misclassified as an octave error.
  * `min_confidence_for_pitch_error` gates the wrong-pitch AND octave-off
    verdicts (both are pitch-reading-dependent), but never gates timing
    checks - timing confidence isn't what `DetectedNote.confidence`
    measures (it's pitch-tracker confidence, not amplitude/onset
    confidence; see transcription.py).
  * A tied-continuation note (`ScoreNote.tied_from_prev=True`) has no new
    attack, so a `MISSED_NOTE` verdict for it (no performed note landed on
    that index) is a false positive, not a real miss - it's treated as
    correct instead. This is a known v1 simplification: it doesn't attempt
    to verify the tie was actually held (e.g. no early release detection).
  * A pair can carry multiple independent mistakes (e.g. wrong pitch AND
    late) - they're orthogonal checks, not mutually exclusive branches.
  * Thresholds in `builtin_profiles()` are placeholders (per
    docs/ARCHITECTURE.md); this module's job is to apply whatever profile
    it's given correctly, not to pick good numbers - that's the eval-harness
    work in the plan's section 5, still future work.
"""

from __future__ import annotations

from .assessment import (
    Assessor,
    AssessmentResult,
    Mistake,
    MistakeType,
    OctavePolicy,
    Severity,
    ToleranceProfile,
)
from .score_align import Alignment
from .score_ingest import Score, ScoreNote
from .transcription import DetectedNote, Transcription


class RuleBasedAssessor(Assessor):
    """v1 Assessor: per-pair rule checks driven entirely by `ToleranceProfile`.

    No detection/alignment logic lives here - it only classifies the
    `Alignment` it's handed. See `score_align.py`'s docstring for the
    explicit alignment/assessment boundary this respects.
    """

    def assess(
        self,
        alignment: Alignment,
        score: Score,
        performance: Transcription,
        profile: ToleranceProfile,
    ) -> AssessmentResult:
        score_by_index = {n.index: n for n in score.notes}
        perf_by_index = {n.index: n for n in performance.notes}
        seconds_per_beat = 60.0 / score.tempo.bpm

        mistakes: list[Mistake] = []
        correct_ref_indices: list[int] = []

        for pair in alignment.pairs:
            # --- gap pairs: one side is None -------------------------------
            if pair.ref_index is not None and pair.performed_index is None:
                score_note = score_by_index.get(pair.ref_index)
                if score_note is not None and score_note.tied_from_prev:
                    # Tied continuation: no new attack expected, not a miss.
                    correct_ref_indices.append(pair.ref_index)
                else:
                    mistakes.append(
                        Mistake(
                            ref_index=pair.ref_index,
                            performed_index=None,
                            type=MistakeType.MISSED_NOTE,
                            severity=Severity.ERROR,
                            detail="No performed note matched this score note.",
                        )
                    )
                continue

            if pair.ref_index is None and pair.performed_index is not None:
                mistakes.append(
                    Mistake(
                        ref_index=None,
                        performed_index=pair.performed_index,
                        type=MistakeType.EXTRA_NOTE,
                        severity=Severity.WARNING,
                        detail="Performed note has no corresponding score note.",
                    )
                )
                continue

            if pair.ref_index is None or pair.performed_index is None:
                # Both None: nothing to assess (defensive; shouldn't occur).
                continue

            # --- real pair: both sides present ------------------------------
            score_note = score_by_index.get(pair.ref_index)
            detected = perf_by_index.get(pair.performed_index)
            if score_note is None or detected is None:
                # Alignment referenced an index this Score/Transcription
                # doesn't have. Not assessable; skip rather than crash.
                continue

            pair_mistakes = self._assess_pair(score_note, detected, profile, seconds_per_beat)
            if pair_mistakes:
                mistakes.extend(pair_mistakes)
            else:
                correct_ref_indices.append(pair.ref_index)

        return AssessmentResult(
            profile_name=profile.name,
            mistakes=mistakes,
            correct_ref_indices=correct_ref_indices,
        )

    @staticmethod
    def _assess_pair(
        score_note: ScoreNote,
        detected: DetectedNote,
        profile: ToleranceProfile,
        seconds_per_beat: float,
    ) -> list[Mistake]:
        mistakes: list[Mistake] = []

        # ---- pitch -------------------------------------------------------
        detected_cents = detected.midi * 100 + detected.cents_offset
        reference_cents = score_note.midi * 100
        cents_off = detected_cents - reference_cents
        low_confidence = detected.confidence < profile.min_confidence_for_pitch_error

        if abs(cents_off) <= profile.pitch_tolerance_cents:
            pass  # pitch correct, nothing to record
        elif low_confidence:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.LOW_CONFIDENCE,
                    severity=Severity.INFO,
                    detail=(
                        f"Pitch confidence {detected.confidence:.2f} is below "
                        f"the {profile.min_confidence_for_pitch_error:.2f} "
                        "threshold; suppressing wrong-pitch verdict, "
                        "flagging for review instead."
                    ),
                    cents_off=cents_off,
                )
            )
        elif detected.pitch_class == score_note.pitch_class and detected.midi != score_note.midi:
            mistakes.extend(
                RuleBasedAssessor._octave_off_mistake(score_note, detected, profile, cents_off)
            )
        else:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.WRONG_PITCH,
                    severity=Severity.ERROR,
                    detail=f"{cents_off:+.0f} cents from the reference pitch.",
                    cents_off=cents_off,
                )
            )

        # ---- timing --------------------------------------------------------
        score_onset_s = score_note.onset_beats * seconds_per_beat
        timing_error_ms = (detected.onset_s - score_onset_s) * 1000.0
        if abs(timing_error_ms) > profile.timing_tolerance_ms:
            mistakes.append(
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.TIMING_LATE if timing_error_ms > 0 else MistakeType.TIMING_EARLY,
                    severity=Severity.WARNING,
                    detail=f"{timing_error_ms:+.0f} ms from the reference onset.",
                    timing_error_ms=timing_error_ms,
                )
            )

        return mistakes

    @staticmethod
    def _octave_off_mistake(
        score_note: ScoreNote,
        detected: DetectedNote,
        profile: ToleranceProfile,
        cents_off: float,
    ) -> list[Mistake]:
        if profile.octave_policy == OctavePolicy.IGNORE:
            return []
        if profile.octave_policy == OctavePolicy.CORRECT_WITH_WARNING:
            return [
                Mistake(
                    ref_index=score_note.index,
                    performed_index=detected.index,
                    type=MistakeType.OCTAVE_OFF,
                    severity=Severity.INFO,
                    detail="Correct pitch class, wrong octave.",
                    cents_off=cents_off,
                )
            ]
        # HARD_ERROR
        return [
            Mistake(
                ref_index=score_note.index,
                performed_index=detected.index,
                type=MistakeType.WRONG_PITCH,
                severity=Severity.ERROR,
                detail=(
                    "Correct pitch class, wrong octave (counted as wrong "
                    "pitch under the HARD_ERROR octave policy)."
                ),
                cents_off=cents_off,
            )
        ]
