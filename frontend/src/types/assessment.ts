/**
 * Shared contract between the (future) backend `assessment` module and the
 * frontend `feedback-ui` module (see music-copilot-mvp-plan.md, modules 6 & 7).
 *
 * This is the shape the real backend is expected to return once the
 * transcription + alignment pipeline exists. For Phase 0 we only have a
 * hardcoded MOCK instance of this type (see mockAssessmentResult.ts) — no
 * real audio analysis happens yet. Keep this file in sync with whatever the
 * backend module ends up serializing; it's the seam between the two halves
 * of the project.
 */

/** Index into the flat, in-order sequence of notes as OSMD/the score exposes them. */
export type NoteIndex = number;

export type ErrorType =
  /** Performed pitch did not match the expected pitch for this note. */
  | 'wrong_pitch'
  /** Note was correct but played notably early/late relative to the beat. */
  | 'timing_slip'
  /** Expected note was not detected in the performance at all. */
  | 'missed_note'
  /** Performance matched the expected note within tolerance. */
  | 'correct';

export type Severity = 'low' | 'medium' | 'high';

export interface NoteAssessment {
  noteIndex: NoteIndex;
  errorType: ErrorType;
  /** Optional human-readable detail, e.g. "played F2, expected G2". */
  detail?: string;
  severity?: Severity;
}

/** Top-level shape of an assessment run, as the backend would return it. */
export interface AssessmentResult {
  /** Identifies which score this assessment was run against. */
  scoreId: string;
  /** One entry per note in the reference score that was evaluated. */
  notes: NoteAssessment[];
  /** ISO timestamp of when the assessment was produced. */
  generatedAt: string;
}
