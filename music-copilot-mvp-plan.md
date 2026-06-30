# Music Copilot — MVP Plan

**Scope of v1:** web app, monophonic instruments (starting with double bass), offline analysis (record → analyze → highlight mistakes on the score). Live mode, polyphonic instruments, and a full notation editor are explicitly deferred to v2, but the module boundaries below are chosen so those can be added without rewrites.

**Assumption used for time estimates:** solo developer, ~10–15 hrs/week, using an AI coding agent (Claude Code) to accelerate implementation. Scale estimates up or down if your actual availability differs.

---

## 1. Architecture: seven independent modules

Each module has a narrow, testable interface so you can swap implementations (e.g. offline DTW → online DTW, browser mic → native mic) without touching the others.

### 1. `score-ingest` — get a score into the system
- Primary path (v1): import clean MusicXML directly, or a typeset PDF you convert once by hand. This unblocks every downstream module without waiting on photo-OMR accuracy.
- Secondary path (v1.5/v2): phone photo → Optical Music Recognition (OMR) → MusicXML.
  - Commercial OMR (Soundslice, PlayScore 2, SmartScore) is currently the most accurate option for photographed sheet music, but these are hosted products without a clean API for embedding in your own app.
  - Open-source engines (Audiveris, oemer, newer projects like Clarity-OMR) are free and embeddable but noticeably less accurate on phone-quality images — skew, shadows, handwriting annotations all hurt them.
  - Double bass parts work in your favor here: they're usually a single staff, bass clef, sparse and monophonic — a much easier OMR target than full piano or ensemble scores.
  - Plan to ship with a manual correction step regardless (OMR will never be perfect). That correction UI is also the natural seed of the "edit sheet music directly" feature you want later — building it once serves both needs.
  - For sparse bass parts specifically, hand-entering the MusicXML for a method-book excerpt may simply be faster than OMR-plus-correction — keep that as a fallback, not just a v1.5 dependency.
- Output: a canonical internal score representation (parsed MusicXML), independent of how it was produced.

### 2. `score-view` — render and (eventually) edit the score
- Use **OpenSheetMusicDisplay (OSMD)** over Verovio. Reasoning: OSMD renders MusicXML natively (the same format your ingest module produces), it's actively maintained, and it exposes a cursor/note API that's well suited to walking through a performance and coloring individual notes — exactly your highlighting use case. Verovio is MEI-native; you'd add a conversion step for no real benefit here, since MEI's strengths (musicological metadata/querying) aren't relevant to this app.
- Neither library is a full notation editor — both are renderers with limited inline edits (visibility, coloring, hiding staves). A real "edit the notes" feature is its own multi-month project (existing browser notation editors like Flat.io took years to mature). Scope that as v2+, built on top of the OMR-correction UI from module 1.

### 3. `audio-input` — capture the recording
- Web Audio API (`getUserMedia` + `AudioWorklet`) for browser mic capture.
- Design it as a streaming interface from day one, even though v1 only uses it in "record fully, then analyze" mode. That way "live mode" later is a matter of feeding the same stream into an incremental analyzer, not a rewrite.
- Keep this module behind a thin interface so a future native phone app (Swift/Kotlin or React Native) can swap in platform mic APIs without changes downstream.

### 4. `transcription` — turn audio into notes
- Monophonic pitch + onset detection. For double bass specifically: the instrument's range (down to E1 ≈ 41 Hz, or B0 ≈ 31 Hz with a low-B extension) needs a tracker and frame size that handle low fundamentals well — plain autocorrelation degrades here. YIN/pYIN-family algorithms and CREPE (a CNN-based tracker) both handle low frequencies more reliably; CREPE is also notably more robust under noise, at the cost of more compute.
- Volume independence is real in principle (pitch trackers detect periodicity, not loudness) but don't treat it as solved: quiet, low-register notes are exactly where the signal itself is weakest (low SNR, weak/missing fundamental) — the hardest case for both onset detection and pitch confidence. Budget real tuning time here. Use the tracker's own per-frame confidence score — not raw amplitude — to decide whether a pitch reading is trustworthy.
- **Octave errors are the dominant double-bass failure mode — plan for them explicitly.** pYIN/CREPE routinely report a note one octave high, or lock onto the first overtone, on instruments with a weak fundamental; double bass is the textbook case. Mitigate with a range prior (clamp candidate pitches to the bass's known register) plus a post-hoc octave-correction pass before notes reach the alignment module.
- Frame size is a real tradeoff, not a free parameter: resolving ~31–41 Hz needs a long analysis window (roughly 50–100ms+), which works directly against onset/timing precision — the exact thing the assessment module measures in milliseconds. Plan to tune this balance rather than pick one default and move on; this is the most likely place Phase 2's estimate slips.
- **Click track / tempo reference:** without one, "rushed" vs. "mistake" is ambiguous for timing-error detection. For v1, use a fixed-tempo excerpt or an optional click track to make timing assessment tractable; revisit free-tempo support later.
- Output: a timestamped sequence of (pitch, onset, offset, confidence).

### 5. `score-align` — line up the performance with the score
- v1: classic offline **DTW** (e.g. `librosa.sequence.dtw` or `dtw-python`), aligning the detected note sequence to the reference notes from the score. Handles tempo flexibility/rubato.
- v2 (live mode): swap in **Online/Incremental DTW (OLTW)** — same conceptual job, computed incrementally as audio streams in. CQT-based front ends with online DTW are the standard approach in the score-following literature and reportedly perform well even on polyphonic input, which is useful if you expand beyond double bass later.
- Keep this behind a single `align(performedNotes, referenceNotes) → alignment` interface now, so the v1→v2 swap doesn't touch the diff or UI modules.
- **Plain DTW is not a solved black box — two real risks to design around:**
  - DTW forces a full monotonic alignment, so it can't natively distinguish "wrong note" from "out of tempo." It gives you alignment, not error classification — wrong notes have to surface as high local cost at a chosen point, which is really the assessment module's job, but the threshold/cost function lives here.
  - DTW breaks when the performer **skips or repeats a section** — a very normal thing to happen in practice — because that violates the monotonicity assumption. Handle this with subsequence-DTW, partial alignment, or an explicit re-sync heuristic. Treat this as a real v1 risk, not something to defer to v2.

### 6. `assessment` — diff the alignment into mistakes
- For each aligned pair: pitch correct/incorrect (configurable cents/semitone tolerance), timing deviation (configurable ms tolerance), missed note, extra note.
- Implement tolerance as **named profiles** (e.g. "Beginner": ±150ms timing, loose pitch tolerance; "Advanced": ±30ms, exact pitch) passed into this module as config — not hardcoded — so adding or adjusting skill levels never touches detection or alignment code.
- **Define the octave-off policy explicitly.** Given how common octave-detection errors are (see module 4), counting an octave-off reading as flat-out "wrong" will generate false positives that erode trust. Recommended v1 policy: same pitch-class but wrong octave = "correct, flagged for review" rather than a hard error.
- Output: a structured mistake list (note index, error type, severity).

### 7. `feedback-ui` — show the mistakes
- Drives OSMD's cursor/coloring API from the assessment module's output: wrong pitch → one color, timing slip → another, missed note → outline/ghost, correct → default/green.

---

## 2. Suggested stack

- **Frontend:** TypeScript, React, OpenSheetMusicDisplay, Web Audio API/AudioWorklet.
- **Backend:** Python (FastAPI) — `music21` or `partitura` for score manipulation, `librosa`/`pYIN` (or CREPE) for pitch tracking, `dtw-python` for alignment. Python's audio/ML library ecosystem is meaningfully better than JS's for this part, and v1 is offline anyway (record in browser, upload, process server-side, return results) so there's no latency pressure forcing client-side analysis yet.
- **Hosting:** a small VPS or a platform like Render/Fly.io/Railway for the backend, static hosting for the frontend. MVP traffic doesn't need anything more than that.
- **Live mode (v2) implication:** client-side WASM pitch detection (for latency) feeding either a client-side or low-latency websocket-based incremental aligner. Plan for this, don't build it yet.

---

## 3. Milestones and rough timeline

| Phase | Work | Estimate |
|---|---|---|
| 0. Setup & spike | Repo scaffolding, module interfaces defined, OSMD rendering a sample bass part, basic mic capture + waveform display. Goal: prove OSMD's highlighting API works end-to-end with *fake* mistake data before any real audio analysis — de-risks the riskiest integration first. | 1–2 weeks |
| 1. Score ingestion v1 | Import clean MusicXML/PDF for a few double-bass method-book excerpts (skip OMR for now). | 1–2 weeks |
| 2. Transcription module | pYIN/CREPE pitch+onset detection tuned for double bass range, including octave-error correction; validate against a hand-labeled set of your own recordings (known-correct and known-wrong takes). Riskiest phase — budget the higher end. | 3–4 weeks |
| 3. Alignment + diff | Offline DTW + configurable tolerance profiles + skip/repeat handling (subsequence-DTW or re-sync heuristic); structured mistake-list output. | 2–3 weeks |
| 4. End-to-end offline flow | Wire assessment output into OSMD highlighting; full pipeline working: record → analyze → see mistakes on the score. | 1–2 weeks |
| **MVP checkpoint** | **Offline, double bass, manually-imported scores, working end-to-end.** | **~8–12 weeks** |
| 5. OMR integration | Phone photo → Audiveris/oemer → MusicXML, plus the manual correction UI. | 2–4 weeks |
| 6. Polish & deploy | Tolerance-profile UI, deploy to your website. | 1–2 weeks |
| **v1 checkpoint** | **Full v1 including photo import.** | **~3–4.5 months part-time** |
| v2 (future) | Live mode (OLTW + client-side WASM pitch tracking + live cursor UX) and a real score editor. Both are materially harder than anything above. | 2–4 months, after v1 ships |

---

## 4. Which Claude model to use

Use **Sonnet 5** as your default for day-to-day implementation — it's the current cost/speed/capability balance point for coding-agent work, and most of this build (UI wiring, CRUD around scores, rendering glue, API plumbing) doesn't need extra reasoning depth.

Reach for **Opus 4.8** selectively, on the handful of genuinely hard design decisions: the alignment module's interface (since it has to support an offline→online swap later) *and specifically its skip/repeat/error-classification logic* — arguably the hardest correctness problem in the whole build — tuning the transcription module for double bass's low frequency range and octave-error correction, and the OMR-correction UI architecture (since it doubles as your future score editor's foundation). Running the whole project on Opus by default would be slower and costlier without much benefit for the bulk of the work.

---

## 5. Evaluation strategy (don't skip this)

Phase 2's "hand-labeled set" needs to become a real, ongoing evaluation harness, not a one-time sanity check:
- Track precision/recall on mistake detection specifically. **False-positive rate matters most** — a system that flags correct notes as wrong destroys trust faster than one that misses real mistakes.
- Build the labeled test set incrementally as you record yourself (known-correct takes, known-wrong takes with deliberate errors, and takes with a skipped/repeated section) — reuse it to regression-test Phases 2 and 3 as you tune thresholds.

## 6. Open questions to revisit

- OMR engine choice (Audiveris vs oemer vs others) is worth a short bake-off once you reach Phase 5 — accuracy on your actual school sheet music matters more than benchmark numbers.
- Exact tolerance values per skill level should come from testing against real bass recordings, not guessed up front.
- Whether server-side or client-side pitch detection makes sense for v1 may change once you measure actual upload/processing latency — revisit before committing to the live-mode architecture.
