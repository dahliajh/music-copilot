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
   the regression set.
5. **Cost-function vs. error-classification split (alignment ↔ assessment).**
   DTW gives alignment, not error classification; the `gap_cost_threshold` lives
   in alignment, but wrong-note severity is assessment's. Keep the boundary
   clean.
6. **Click-track requirement (ingest/tempo).** v1 assessment assumes a fixed
   tempo / optional click track so "rushed" vs. "wrong" is well-defined. Free
   tempo is deferred.
7. **False-positive rate is the headline metric (assessment).** Build the
   labeled eval harness (plan section 5) and track precision/recall, FP-first.
