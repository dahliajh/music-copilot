import type { AssessmentResult } from '../types/assessment';

/**
 * FAKE / PLACEHOLDER DATA — Phase 0 spike only.
 *
 * There is no real transcription/alignment pipeline yet (see modules 4-6 in
 * music-copilot-mvp-plan.md). This object stands in for what the backend
 * `assessment` module will eventually return after analyzing a recording
 * against `sample-bass-excerpt.musicxml`. It exists purely to prove that
 * OSMD's cursor/note API can be driven by a structured result object shaped
 * like this one. Delete/replace this file once the real backend exists —
 * the AssessmentResult type in src/types/assessment.ts is the part that
 * should survive.
 *
 * The excerpt has 10 notes (index 0-9). We fake:
 *  - index 2 -> wrong pitch (red)
 *  - index 5 -> timing slip (orange)
 *  - index 7 -> missed note (grey/outlined)
 *  - everything else -> correct (green)
 */
export const mockAssessmentResult: AssessmentResult = {
  scoreId: 'sample-bass-excerpt',
  generatedAt: '2026-06-30T00:00:00.000Z',
  notes: [
    { noteIndex: 0, errorType: 'correct' },
    { noteIndex: 1, errorType: 'correct' },
    {
      noteIndex: 2,
      errorType: 'wrong_pitch',
      detail: 'expected A2, heard A#2',
      severity: 'high',
    },
    { noteIndex: 3, errorType: 'correct' },
    { noteIndex: 4, errorType: 'correct' },
    {
      noteIndex: 5,
      errorType: 'timing_slip',
      detail: 'played ~140ms late',
      severity: 'medium',
    },
    { noteIndex: 6, errorType: 'correct' },
    {
      noteIndex: 7,
      errorType: 'missed_note',
      detail: 'no onset detected near expected beat',
      severity: 'high',
    },
    { noteIndex: 8, errorType: 'correct' },
    { noteIndex: 9, errorType: 'correct' },
  ],
};
