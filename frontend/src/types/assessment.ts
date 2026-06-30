/**
 * Shared contract between the backend `assessment` module
 * (backend/app/modules/assessment.py) and the frontend feedback UI.
 *
 * This mirrors the backend's pydantic models field-for-field, including
 * field names (snake_case) and nullability, so the JSON the real API
 * returns can be typed directly with no renaming/translation layer. If
 * this ever drifts from backend/app/modules/assessment.py, update both
 * together — that file is the source of truth for the shape.
 *
 * The backend's natural shape is SPARSE: it lists mistakes plus a separate
 * array of correct note indices, rather than one entry per note. The
 * frontend's rendering code wants a DENSE per-note view (one lookup per
 * note, defaulting to "correct"). That densification is a frontend-only
 * concern, kept out of this file — see `densifyAssessment` in
 * `frontend/src/score/assessmentAdapter.ts`. Don't reintroduce a dense
 * wire type here; adapt at the UI edge instead.
 */

/** 0-based position in the monophonic part — matches backend ScoreNote.index. */
export type NoteIndex = number;

/** Mirrors backend MistakeType (assessment.py). */
export type MistakeType =
  /** Performed pitch did not match the expected pitch for this note. */
  | 'wrong_pitch'
  /** Same pitch-class, wrong octave. Default policy treats this as a warning, not a hard error. */
  | 'octave_off'
  /** Correct pitch, played notably before the expected beat. */
  | 'timing_early'
  /** Correct pitch, played notably after the expected beat. */
  | 'timing_late'
  /** Score note with no performed match. */
  | 'missed_note'
  /** Performed note with no score match — has no `ref_index`. */
  | 'extra_note'
  /** Transcription confidence too low to trust a pitch verdict; flagged for review. */
  | 'low_confidence';

/** Mirrors backend Severity (assessment.py). */
export type Severity = 'info' | 'warning' | 'error';

/** Mirrors backend Mistake (assessment.py) field-for-field. */
export interface Mistake {
  ref_index: NoteIndex | null;
  performed_index: NoteIndex | null;
  type: MistakeType;
  severity: Severity;
  detail: string | null;
  cents_off: number | null;
  timing_error_ms: number | null;
}

/** Mirrors backend AssessmentResult (assessment.py) field-for-field. */
export interface AssessmentResult {
  profile_name: string;
  mistakes: Mistake[];
  /** Note indices with no mistake — i.e. assessed and correct. */
  correct_ref_indices: NoteIndex[];
}
