import { useEffect, useRef, useState } from 'react';
import { OpenSheetMusicDisplay } from 'opensheetmusicdisplay';
import sampleExcerptUrl from '../assets/sample-bass-excerpt.musicxml?url';
import type { AssessmentResult } from '../types/assessment';
import { mockAssessmentResult } from './mockAssessmentResult';
import { densifyAssessment, type DisplayErrorType } from './assessmentAdapter';

interface ScoreViewProps {
  /**
   * Assessment result to render as highlights. Defaults to the Phase 0 mock
   * data. A real caller will eventually pass in whatever the backend
   * `assessment` module returns, typed against AssessmentResult.
   */
  assessment?: AssessmentResult;
}

/** Color/border mapping for each display category. Kept in one place so the
 * legend and the actual highlight logic can't drift apart. */
const ERROR_STYLE: Record<DisplayErrorType, { color: string; label: string }> = {
  correct: { color: '#2e7d32', label: 'Correct' },
  wrong_pitch: { color: '#d32f2f', label: 'Wrong pitch' },
  octave_off: { color: '#f9a825', label: 'Octave off' },
  timing_early: { color: '#1976d2', label: 'Timing slip (early)' },
  timing_late: { color: '#ef6c00', label: 'Timing slip (late)' },
  missed_note: { color: '#9e9e9e', label: 'Missed note' },
  low_confidence: { color: '#7b1fa2', label: 'Flagged for review' },
};

export function ScoreView({ assessment = mockAssessmentResult }: ScoreViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function renderScore() {
      if (!containerRef.current) return;
      try {
        const osmd = new OpenSheetMusicDisplay(containerRef.current, {
          autoResize: true,
          backend: 'svg',
          drawTitle: true,
        });
        osmdRef.current = osmd;

        const response = await fetch(sampleExcerptUrl);
        const musicXmlText = await response.text();
        await osmd.load(musicXmlText);
        if (cancelled) return;
        osmd.render();

        applyHighlights(osmd, assessment);
        setStatus('ready');
      } catch (err) {
        console.error('Failed to render score', err);
        if (!cancelled) {
          setErrorMessage(err instanceof Error ? err.message : String(err));
          setStatus('error');
        }
      }
    }

    renderScore();
    return () => {
      cancelled = true;
    };
  }, [assessment]);

  return (
    <div className="score-view">
      <div className="score-view__legend">
        {(Object.keys(ERROR_STYLE) as DisplayErrorType[]).map((type) => (
          <span key={type} className="score-view__legend-item">
            <span
              className="score-view__legend-swatch"
              style={{ backgroundColor: ERROR_STYLE[type].color }}
            />
            {ERROR_STYLE[type].label}
          </span>
        ))}
      </div>
      {status === 'loading' && <p className="score-view__status">Loading score…</p>}
      {status === 'error' && (
        <p className="score-view__status score-view__status--error">
          Failed to render score: {errorMessage}
        </p>
      )}
      <div ref={containerRef} className="score-view__container" />
    </div>
  );
}

/**
 * Walks the OSMD-rendered note sequence in score order and applies a fake
 * mistake's color/outline to each note referenced by the (densified)
 * assessment. This is the core thing Phase 0 needs to prove out: that OSMD
 * exposes enough of a per-note API to drive highlighting from an external
 * (eventually backend-supplied) data structure.
 *
 * The backend's wire shape is sparse (mistakes + correct_ref_indices); see
 * `densifyAssessment` for the conversion to the per-note lookup used here.
 */
function applyHighlights(osmd: OpenSheetMusicDisplay, assessment: AssessmentResult) {
  const { byNoteIndex } = densifyAssessment(assessment);

  // Flatten all GraphicalNotes across all pages/systems/measures/staff
  // entries in score order, matching the note-index convention documented
  // in sample-bass-excerpt.musicxml's inline comments.
  let flatIndex = 0;
  const graphicSheet = osmd.GraphicSheet;

  for (const musicPage of graphicSheet.MusicPages) {
    for (const musicSystem of musicPage.MusicSystems) {
      for (const staffLine of musicSystem.StaffLines) {
        for (const measure of staffLine.Measures) {
          for (const staffEntry of measure.staffEntries) {
            for (const voiceEntry of staffEntry.graphicalVoiceEntries) {
              for (const note of voiceEntry.notes) {
                // Skip rests if they're represented as notes with no pitch;
                // our fixture is monophonic with no rests, but guard anyway.
                const sourceNote = note.sourceNote;
                if (sourceNote && sourceNote.isRest()) {
                  continue;
                }

                const display = byNoteIndex.get(flatIndex);
                const errorType: DisplayErrorType = display?.errorType ?? 'correct';
                const style = ERROR_STYLE[errorType];

                // OSMD's per-note coloring hook: NoteheadColor is read by the
                // SVG/Canvas backends at render time when re-rendered, and
                // can also be nudged directly via the note's graphical
                // representation for an already-rendered sheet.
                sourceNote.NoteheadColor = style.color;

                if (errorType === 'missed_note') {
                  // Extra visual treatment beyond color for missed notes:
                  // OSMD doesn't have a built-in "ghost note" mode, so we
                  // approximate "outlined/greyed" by also marking it filled=false
                  // where the engraving rules respect that flag.
                  sourceNote.PrintObject = true;
                }

                flatIndex += 1;
              }
            }
          }
        }
      }
    }
  }

  // Re-render so the NoteheadColor changes take effect.
  osmd.render();
}
