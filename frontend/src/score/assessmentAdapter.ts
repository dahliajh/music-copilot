import type {
  AssessmentResult,
  Mistake,
  MistakeType,
  NoteIndex,
  Severity,
} from '../types/assessment';

/**
 * Frontend-only display categories for per-note highlighting: every backend
 * `MistakeType` that can be pinned to a single score note (has a
 * `ref_index`), plus `'correct'` for notes with no mistake at all.
 *
 * `extra_note` is deliberately excluded — those mistakes have no
 * `ref_index` (they're performed notes with no score match), so they can't
 * be drawn as a highlight on a score note. See `extraNotes` below.
 */
export type DisplayErrorType = Exclude<MistakeType, 'extra_note'> | 'correct';

export interface NoteDisplay {
  errorType: DisplayErrorType;
  detail?: string | null;
  severity?: Severity;
}

export interface DensifiedAssessment {
  /** One entry per note that has something to show. Any note index absent
   * here had no mistake AND wasn't listed in `correct_ref_indices` —
   * callers should still default it to 'correct' defensively, but a
   * fully-assessed result shouldn't have gaps. */
  byNoteIndex: Map<NoteIndex, NoteDisplay>;
  /** Mistakes with no `ref_index` (extra/unmatched performed notes). Not
   * renderable as a note highlight; surfaced separately for future UI. */
  extraNotes: Mistake[];
}

/**
 * Converts the backend's sparse `AssessmentResult` into the dense per-note
 * lookup the score-rendering code wants. This is the one place that should
 * know about both shapes — keep `ScoreView` ignorant of the sparse wire
 * format so the two sides can't drift apart silently again.
 */
export function densifyAssessment(result: AssessmentResult): DensifiedAssessment {
  const byNoteIndex = new Map<NoteIndex, NoteDisplay>();
  const extraNotes: Mistake[] = [];

  for (const mistake of result.mistakes) {
    // extra_note mistakes never carry a ref_index by construction, but TS
    // can't infer that correlation from the wire type alone — check both so
    // the type narrows correctly for the .set() below.
    if (mistake.type === 'extra_note' || mistake.ref_index == null) {
      extraNotes.push(mistake);
      continue;
    }
    byNoteIndex.set(mistake.ref_index, {
      errorType: mistake.type,
      detail: mistake.detail,
      severity: mistake.severity,
    });
  }

  for (const index of result.correct_ref_indices) {
    if (!byNoteIndex.has(index)) {
      byNoteIndex.set(index, { errorType: 'correct' });
    }
  }

  return { byNoteIndex, extraNotes };
}
