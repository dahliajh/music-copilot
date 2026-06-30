import type { AssessmentResult } from '../types/assessment';

/**
 * FAKE / PLACEHOLDER DATA — Phase 0 spike only.
 *
 * There is no real transcription/alignment pipeline yet (see modules 4-6 in
 * music-copilot-mvp-plan.md). This object stands in for what the backend
 * `assessment` module will eventually return after analyzing a recording
 * against `sample-bass-excerpt.musicxml`. It exists purely to prove that
 * OSMD's cursor/note API can be driven by a structured result object shaped
 * like this one.
 *
 * Shaped EXACTLY like the real backend would serialize it — sparse
 * `mistakes` + `correct_ref_indices`, mirroring `_mock_assessment` in
 * backend/app/main.py — not the dense per-note array this file used to
 * use. See `src/score/assessmentAdapter.ts` for how `ScoreView` turns this
 * into the per-note lookup it actually renders from.
 *
 * The excerpt has 10 notes (index 0-9). We fake:
 *  - index 2 -> wrong pitch (red)
 *  - index 5 -> timing slip, played late (orange)
 *  - index 7 -> missed note (grey/outlined)
 *  - everything else -> correct (green)
 */
export const mockAssessmentResult: AssessmentResult = {
  profile_name: 'beginner',
  mistakes: [
    {
      ref_index: 2,
      performed_index: 2,
      type: 'wrong_pitch',
      severity: 'error',
      detail: 'expected A2, heard A#2',
      cents_off: null,
      timing_error_ms: null,
    },
    {
      ref_index: 5,
      performed_index: 5,
      type: 'timing_late',
      severity: 'warning',
      detail: 'played ~140ms late',
      cents_off: null,
      timing_error_ms: 140,
    },
    {
      ref_index: 7,
      performed_index: null,
      type: 'missed_note',
      severity: 'error',
      detail: 'no onset detected near expected beat',
      cents_off: null,
      timing_error_ms: null,
    },
  ],
  correct_ref_indices: [0, 1, 3, 4, 6, 8, 9],
};
