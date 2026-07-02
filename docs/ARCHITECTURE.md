# Music Copilot — Backend Architecture (v1 skeleton)

This document describes the four backend module contracts under
`backend/app/modules/` and the FastAPI skeleton that composes them. It is the
companion to `music-copilot-mvp-plan.md`; read the plan's section 1 first.

The guiding rule is the plan's: **each module has a narrow, testable interface
so implementations can be swapped (offline DTW → online DTW, MusicXML → OMR)
without touching callers.** Every contract here is an abstract base class
(`abc.ABC`) plus pydantic data types. The FastAPI app depends only on these
types, with mock implementations, to prove the seams compose end-to-end before
any real analysis exists.

## Data flow

```
score-ingest ──► Score ─────────────┐
                                     ▼
audio ──► transcription ──► Transcription ──► score-align ──► Alignment ──► assessment ──► AssessmentResult ──► feedback-ui
```

`score-view`, `audio-input`, and `feedback-ui` are frontend modules; only the
four backend contracts live here.

---

## 1. `score_ingest.py`

**Job:** raw external score (MusicXML / converted PDF / future OMR photo) → one
canonical `Score`.

**Contract:** `ScoreIngester.ingest(raw, source_format) -> ScoreIngestResult`.

**Key types:** `Score` (id, tempo, list of `ScoreNote`), `ScoreNote`
(MIDI + explicit `pitch_class`, time in **beats**), `TempoReference`,
`ScoreSourceFormat`.

**Why this shape:**
- The canonical `Score` is decoupled from *how* it was produced. v1 ingests
  clean MusicXML; v1.5/v2 add an `OmrIngester` that yields the *same* `Score`,
  so nothing downstream changes. (Plan: "Output: a canonical internal score
  representation, independent of how it was produced.")
- `ScoreNote.needs_review` + `Score.needs_manual_correction` anticipate the
  **manual-correction step** OMR will always require. A corrected score is just
  another `Score` fed back in — the seed of the future score editor.
- Pitch is MIDI + explicit `pitch_class` so alignment/assessment can do
  octave/pitch-class math without re-parsing note names. The octave-off policy
  in assessment keys directly off `pitch_class`.
- Score time is in **beats**, not seconds, because the score is tempo-free;
  `TempoReference` (the v1 click-track requirement) converts to seconds. A
  future per-beat tempo map slots into `beat_seconds` without a contract change.

---

## 2. `transcription.py`

**Job:** mono audio → timestamped `DetectedNote`s, each with a per-note pitch
**confidence**.

**Contracts (deliberately three):**
- `PitchTracker.transcribe(...)` — raw pYIN/CREPE detection, **no** octave
  correction.
- `OctaveCorrector.correct(...)` — a **separate, swappable** octave-correction
  pass.
- `Transcriber.run(...)` — facade composing the two; the only thing callers
  depend on.

**Why this shape:**
- **Octave errors are the dominant double-bass failure mode** (plan, module 4).
  Correction is split into its own contract rather than baked into the tracker,
  so the policy (range-clamp, median smoothing, overtone heuristics) can be
  iterated and unit-tested in isolation against hand-labeled recordings.
  `DetectedNote.octave_corrected` records when a shift happened.
- `DetectedNote.confidence` is first-class and explicitly the **tracker's own
  confidence, not amplitude** (plan: "Use the tracker's own per-frame confidence
  score — not raw amplitude"). Downstream stages down-weight low-confidence
  notes; assessment can flag-for-review instead of hard-failing them.
- `TranscriptionConfig.frame_size_ms` is an explicit knob, not a hidden default,
  because of the **low-frequency vs. timing-precision tradeoff** (~31–41 Hz
  needs ~50–100 ms windows, which fights onset precision). `min_midi`/`max_midi`
  encode the **range prior** that suppresses octave jumps.

### Implementation notes (pyin_transcriber.py)

The v1 implementation (`PyinPitchTracker` + `RangeClampOctaveCorrector` +
`PyinTranscriber` facade) exists as of the tempo-elastic-timing commit's
follow-up. `RangeClampOctaveCorrector` is exactly the simplest policy this
section names above — shift by whole octaves into `[min_midi, max_midi]`,
nothing smarter yet.

The real design problem turned out to be **note segmentation, not pitch
estimation** — deciding where one note ends and the next begins. Amplitude-
based onset detection (`librosa.onset.onset_detect`) is accurate when it
finds a boundary, but bowed-string **slurs produce no new attack transient**,
so it silently misses exactly those boundaries. Validated against a real
recording (Rabbath Étude No. 1, played freely): pure onset detection
under-segmented a 62-note passage by roughly a third, concentrated at the
slurred spots the player had flagged in advance. `_split_oversized_segments`
fixes this without needing to know in advance which notes are slurred: a
segment much longer than the piece's own median note duration probably
swallowed more than one note, and only *those* segments get sub-split by
pitch-change (the one signal a slur still leaves). Tried the reverse first
(segment everything by smoothed pitch-change) — it caught more note events
but was *less* pitch-accurate and added a systematic onset-timing lag (the
smoothing needed to reject frame-to-frame jitter also delays exactly when a
boundary gets reported), which is what motivated the tempo-elastic timing
fix in `rule_based_assessor.py` in the first place. The hybrid beat both
pure approaches on the real recording (see STATUS.md for the numbers).

Near-zero-confidence segments (`MIN_CONFIDENCE = 0.03`) are dropped
entirely rather than reported as low-confidence notes — validated as real
pre-performance noise floor (bow/tuning noise), not musical content, by
checking the raw audio energy directly (no silence gap existed where the
noise-floor segments were).

Wired into the API via `/performance/analyze_recording` (multipart audio
upload), kept separate from the original JSON-only `/performance/analyze`
(still `_mock_transcription()`) so nothing that already depends on that
endpoint's contract changes. Audio-format decoding (WAV → mono PCM16)
happens at the HTTP boundary in `main.py`, not inside `PitchTracker`, so
the tracker's own contract (raw PCM + explicit sample rate) stays simple
to unit-test with synthesized sine-wave audio — no real recording is
committed to this repo to test against.

Not yet validated: multiple pieces/tempos, or the `OVERSIZED_FACTOR`/
`MIN_CONFIDENCE` thresholds against anything beyond the one real
recording used during development. This is exactly the labeled eval
harness gap risk area #7 already calls out.

---

## 3. `score_align.py` (the hard one)

**Job:** map performed notes ↔ reference notes. Produces an **alignment, not an
error classification**.

**Contract:** `ScoreAligner.align(performance, score, config) -> Alignment`,
plus `supports_mode()` and an `align_incremental()` hook for v2 streaming.

**Why this shape:**
- **v1→v2 swap without touching callers.** `AlignMode` (`OFFLINE` / `ONLINE`)
  selects the computation mode; the return type `Alignment` is identical for
  both. v1 ships `OfflineDtwAligner`; v2 ships `OnlineDtwAligner` (OLTW) that
  also implements `align_incremental()` for streaming. Callers never branch on
  mode. (Plan: "Keep this behind a single `align(...)` interface now so the
  v1→v2 swap doesn't touch the diff or UI modules.")
- **Skip/repeat handling is first-class, not deferred.** Plain DTW forces a full
  monotonic alignment and **breaks when the performer skips or repeats a
  section** — a normal occurrence the plan flags as a real v1 risk.
  `AlignStrategy` exposes `GLOBAL_DTW` (baseline), `SUBSEQUENCE_DTW`, and
  `RESYNC`. `Alignment` carries `segments` (multiple monotonic runs) and
  explicit `skip_repeat_spans`, so a skip is represented structurally rather
  than smeared into a bad monotonic path.
- **Gaps are representable.** A `NotePair` may have a `None` index → a missed
  score note or an extra performed note, instead of forcing every note to match.
  `AlignConfig.gap_cost_threshold` is the local-cost ceiling above which a pair
  is left unmatched. The cost function lives here (an alignment concern) even
  though wrong-note *severity* is assessment's job — exactly the split the plan
  calls out.
- `Alignment.is_partial` supports incremental online results not yet finalized.

### Implementation notes (OfflineDtwAligner)

The v1 implementation lives in `offline_dtw_aligner.py`. It rolls its own
Needleman–Wunsch-style DP table rather than calling `dtw-python`, because we need
first-class **gap moves** and **segment** control that an off-the-shelf monotonic
warping path doesn't expose. Each cell has three predecessors: diagonal (match),
"left" (consume a score note with no performed match → *missed note*), "up"
(consume a performed note with no score match → *extra note*). Local cost is
`pitch_cost_weight · pitch + timing_cost_weight · timing`, where pitch is a
**pitch-class-aware** distance — circular pitch-class distance (0–6 semitones)
plus a small per-octave penalty, so an octave error costs far less than an
unrelated wrong note and still *aligns* (octave correction is downstream).
Timing is the onset deviation converted to beats via `tempo.bpm`. Any candidate
match whose local cost exceeds `gap_cost_threshold` is forbidden as a match, so
the DP routes around it as two one-sided `NotePair`s instead of a forced bad
match. A small match-reward (`gap·0.5`) is subtracted from legitimate matches so
the optimal path threads through every real correspondence rather than an
equal-cost degenerate route that drops a genuinely-matching note into a gap.
`GLOBAL_DTW` anchors both reference boundaries; `SUBSEQUENCE_DTW` frees the
reference prefix/suffix so the performance can match a contiguous sub-span.

`RESYNC` runs a **global** first pass (so a skip surfaces as a long interior run
of missed score notes and a repeat as a run of extra performed notes), finds the
first contiguous run of *bad* steps longer than `resync_window` — a step is bad
if it is a gap pair **or** a match whose cost exceeds `gap·0.5` — then splits the
*performance* at that run and re-aligns each slice against the whole score with
subsequence DTW, letting the "after" slice re-anchor forward (skip) or backward
(repeat). Skip vs. repeat is decided from the re-anchor points: if the after-slice
anchors forward leaving an un-covered interior span, that span is `"skipped"`; if
it anchors at or before the before-slice's last matched note, it is `"repeated"`.

Resolved vs. still approximate (re: "Known risk areas" 4 & 5 below): item 5 (the
cost-function vs. error-classification split) is cleanly **resolved** — the cost
function and `gap_cost_threshold` live here and only ever produce an alignment;
no severity judgement leaks in. Item 4 (skip/repeat breakage) is **largely
resolved for skips** — they are detected and re-anchored robustly, verified by
tests. **Repeat detection is heuristic and the weaker half**: backward
re-anchoring is a strong signal that *a* repeat happened and the surplus notes
are surfaced structurally (a `"repeated"` span and/or extra one-sided pairs
rather than forced matches), but proving the replay re-covers a *specific*
earlier score span exactly would need a dedicated second alignment pass against
the prior region — future work. Detection thresholds (`resync_window`,
`gap_cost_threshold`, the `gap·0.5` badness/reward fractions) are also unvalidated
against real recordings and belong in the eval harness (plan section 5).

---

## 4. `assessment.py`

**Job:** diff an `Alignment` into a structured `Mistake` list.

**Contract:** `Assessor.assess(alignment, score, performance, profile)
-> AssessmentResult`.

**Why this shape:**
- **Tolerances are a config object, not constants.** `ToleranceProfile`
  (`pitch_tolerance_cents`, `timing_tolerance_ms`, …) is passed in.
  `builtin_profiles()` ships `beginner` and `advanced`; adding/tuning a level
  never touches detection or alignment code. (Values are placeholders to be
  tuned against real recordings — plan section 6.)
- **The octave-off policy is an explicit enum**, `OctavePolicy`, on the profile.
  v1 default is `CORRECT_WITH_WARNING` (same pitch-class, wrong octave →
  `MistakeType.OCTAVE_OFF` at `Severity.INFO`, not a hard error), per the plan's
  recommendation, because false-positives erode trust faster than misses.
  `HARD_ERROR` and `IGNORE` are available for tuning.
- `min_confidence_for_pitch_error` suppresses a wrong-pitch verdict when the
  transcription reading itself is untrustworthy, flagging for review instead.
- Output keys mistakes to `ref_index` for the feedback-ui coloring API and lists
  `correct_ref_indices` so the UI can color the rest green cheaply.

---

## Persistence seam (`score_store.py`) — not one of the plan's 7 modules

`/score/import` and `/performance/analyze` are separate HTTP requests, so
something has to hold a `Score` between them. `ScoreStore` is a narrow
contract (`save`/`get`) for that, following the same swap-without-touching-
callers pattern as everything else here. v1 (`InMemoryScoreStore`) is a
plain in-process dict — explicitly not durable across restarts and not
shared across worker processes; fine for this single-process skeleton,
not for production. `/performance/analyze` falls back to a fixed demo
score when `score_id == "mock-reference"` so the octave-off demo scenario
still works without an import first; any other unrecognized `score_id` is
a 404, not a silent fallback.

---

## Known risk areas for future implementation

These are explicitly carried from the plan's post-review notes; the contracts
make room for them but **do not solve them** — implementation work must:

1. **Octave-detection errors (transcription).** The dominant bass failure mode.
   Use the range prior (`min_midi`/`max_midi`) plus a real `OctaveCorrector`
   before notes reach alignment. Validate against hand-labeled recordings.
2. **Frame-size / timing-precision tradeoff (transcription).** `frame_size_ms`
   must be tuned, not defaulted-and-forgotten; this is the most likely place
   Phase 2's estimate slips.
3. **Low-SNR quiet low notes (transcription).** Lowest confidence is exactly
   where the signal is weakest; budget tuning time. Drive trust off
   `confidence`, not amplitude.
4. **DTW skip/repeat breakage (alignment).** Implement `SUBSEQUENCE_DTW` /
   `RESYNC` for real, not just `GLOBAL_DTW`. Include skipped/repeated takes in
   the regression set. **Found and fixed a real bug here** via the full
   9-line Rabbath étude validation: `SUBSEQUENCE_DTW`'s free reference
   prefix/suffix (correctly unpenalised during the DP's optimal-path search,
   see `_dp_align`'s docstring) meant boundary score notes with no cheap
   match left the DP with literally no pair at all — not even a
   `missed_note` gap — because ending the match early always costs less than
   walking them as an explicit gap. The etude's closing whole-note chord and
   the note before it vanished from assessment entirely with zero verdict.
   This is indistinguishable, from the DP's perspective alone, from the
   performer genuinely having stopped early — which is exactly the case
   `SUBSEQUENCE_DTW`'s free boundary exists to support — so it's an honest
   ambiguity, not a simple "score note vs. silence" bug. Fixed in `align()`
   (not `_dp_align` itself, and specifically NOT applied inside
   `_align_resync`'s internal per-slice calls) by backfilling any reference
   index untouched by the DP's chosen path as a zero-cost `missed_note`-
   eligible gap pair, so every score note always gets *some* verdict.
   RESYNC is deliberately excluded from this backfill: its skipped-middle
   sections are already reported as one structured `SkipRepeatSpan`, and
   applying the same backfill there would double-report the same skip as
   both a span AND a wall of individual `missed_note` mistakes. See
   `test_trailing_notes_never_detected_are_still_flagged_missed` and
   `test_resync_skipped_middle_not_double_reported_by_boundary_backfill` in
   `test_offline_dtw_aligner.py`.
5. **Cost-function vs. error-classification split (alignment ↔ assessment).**
   DTW gives alignment, not error classification; the `gap_cost_threshold` lives
   in alignment, but wrong-note severity is assessment's. Keep the boundary
   clean.
6. **Click-track requirement (ingest/tempo) — partially resolved.** v1
   originally assumed a fixed tempo / click track so "rushed" vs. "wrong" is
   well-defined. `RuleBasedAssessor` now builds a `_TempoCurve` from the
   alignment's own matched notes (leave-one-out local interpolation) instead
   of a single fixed `score.tempo.bpm`, so free/expressive playing doesn't
   generate runaway false "late" verdicts as real pace drifts from the
   printed tempo — validated against a real recording (Rabbath Étude No. 1,
   lines 1-2): correct-note coverage went from 2/62 to 14/62 and
   timing-related mistakes dropped from 21 to 9 on the same transcription,
   changing nothing but the timing model. Still NOT a full solution: it's a
   local-neighbor smoothing heuristic, not a tempo model, so it can't
   distinguish "decelerating through a hard passage" from "one rushed note"
   any better than eyeballing a few neighbors could, a single badly-mistimed
   note visibly distorts its immediate neighbors' local estimates too (see
   `_TempoCurve`'s docstring and `test_note_genuinely_out_of_place_is_still_
   caught_despite_drift`'s "containment, not full isolation" assertions),
   and it still can't tell an intentional pause from a mistake. A true
   tempo-tracking model (e.g. a smoothed/regularized tempo curve fit, or a
   proper local-warping model) is future work if this heuristic proves
   insufficient in the eval harness below.
7. **False-positive rate is the headline metric (assessment).** Build the
   labeled eval harness (plan section 5) and track precision/recall, FP-first.
