# Music Copilot — Status

_Last updated: 2026-07-01, after formalizing Phase 2 (real transcription) into committed code._

## Phase 2 (transcription) is now real code, not just a scratch script

`PyinPitchTracker` + `RangeClampOctaveCorrector` + `PyinTranscriber` facade
(`backend/app/modules/pyin_transcriber.py`) turn today's earlier scratch
onset-detection experiments into real, tested implementations of the
`transcription.py` contracts — the last of the plan's 7 modules to go from
mock to real. Design (the note-SEGMENTATION problem, not pitch estimation,
turned out to be the hard part — amplitude onsets miss slurred note
boundaries entirely) is written up in `docs/ARCHITECTURE.md`'s
"Implementation notes (pyin_transcriber.py)".

Wired into a new endpoint, `/performance/analyze_recording` (multipart audio
upload) — kept separate from the original JSON-only `/performance/analyze`
(still `_mock_transcription()`) so that endpoint's existing contract doesn't
change for anything already depending on it. Real callers with audio should
use the new endpoint.

13 new tests (`test_pyin_transcriber.py`, `test_analyze_recording_endpoint.py`)
using synthesized sine-wave audio — no real recording is committed to this
repo (it's Naveen's personal performance audio and wouldn't be a stable CI
fixture anyway). Sine waves can't validate real-world segmentation quality;
that validation happened separately against the real Rabbath recording (see
below) and is what the module's design choices (`_split_oversized_segments`,
`MIN_CONFIDENCE`, etc.) are actually based on. 73 backend tests passing.

## Tempo-elastic timing assessment (HEAD `dedd346`)

## Tempo-elastic timing assessment (HEAD `dedd346`)

`RuleBasedAssessor` checked timing against a single fixed tempo
(`score.tempo.bpm`) for the whole performance — fine for a click-track take,
but real free/expressive playing naturally speeds up and slows down, and a
fixed clock turns that normal variation into runaway false "late" verdicts.
Found via the real Rabbath-recording test below: timing mistakes dominated
even though pitch detection was reasonably accurate, and the drift was
systematic (grew over time, no actual silence gap in the audio), not noise.

Added `_TempoCurve`: an empirical onset_beats→onset_s mapping built from the
alignment's own matched notes (leave-one-out interpolation), replacing the
fixed-bpm check. Falls back to fixed-tempo behavior with fewer than 2 other
matched anchors nearby, which is why every pre-existing test still passes
unchanged. On the same real transcription from the Rabbath test, correct-note
coverage went from 2/62 to 14/62 and timing mistakes dropped from 21 to 9 —
changing nothing but the timing model. Explicitly not a full tempo-tracking
model (see `docs/ARCHITECTURE.md`'s "Known risk areas" #6 for what's still
unsolved: can't distinguish a decelerating passage from one rushed note any
better than eyeballing neighbors would; can't tell a pause from a mistake).
58/58 backend tests passing.

## Important fix: double bass is a transposing instrument (HEAD `2b9fd5d`)

Double bass is notated a full octave above where it actually sounds (like
guitar). `MusicXMLIngester` was reading music21's raw parsed pitch — **written**
pitch — directly into `ScoreNote.midi`, but that field is compared downstream
against a real pitch tracker's output from actual audio, which is necessarily
**sounding** pitch. Every real double-bass MusicXML export declares this via
`<transpose><octave-change>-1</octave-change></transpose>`; the ingester wasn't
applying it. Fixed by calling `parsed.toSoundingPitch()` right after parsing
(verified it's a no-op for non-transposing sources, e.g. the Bach chorale vocal
bass line, so nothing else broke). `simandl_etude1_mm1-3.musicxml` now declares
the transposition properly and its test asserts real sounding-pitch MIDI values.
Found via Naveen's own account of his recording: a note written as A3 in the
piece he's playing sounds as A2 (open A string). 55/55 backend tests passing.

## Note on concurrency (resolved)

Earlier this session, a different concurrent session was working on this same project and its edits collided with this one (once in code, once in this file). Naveen confirmed the cause — a separate conversation he'd started earlier plus an hourly scheduled trigger — and has since stopped the trigger. Shouldn't recur, but if status info ever looks internally inconsistent again, trust `git log`/`git ls-remote` on GitHub over this file.

## Where the code lives

- **Source of truth: GitHub.** `https://github.com/dahliajh/music-copilot`, master branch. **Confirmed HEAD: `aae7a4e`** (10 commits), verified via `git ls-remote` immediately before this session's push (not just assumed).
- `/tmp/music-copilot` inside the sandbox is the working clone (ephemeral). `music-copilot.bundle` in this project folder is a secondary offline backup.
- **What you can see in this folder:** `backend/`, `frontend/`, `docs/ARCHITECTURE.md`, `.github/workflows/ci.yml` — plain-file mirror, no `.git` here (see `cowork-mounted-folder-git` skill).

## What's built — all real, all tested, CI green on `aae7a4e`

- **Phase 0 (spike):** OSMD rendering, mic capture + waveform, module contracts as ABCs.
- **Phase 1 (score ingestion):** `MusicXMLIngester` (music21). Handles the happy path plus graceful degradation (missing tempo, ties, rests, chords/multi-voice/multi-part) with warnings instead of hard failures. Wired into `/score/import`.
- **Phase 3 (alignment + assessment):** `OfflineDtwAligner` (hand-rolled DP so gap moves and RESYNC skip/repeat handling are first-class) and `RuleBasedAssessor` (tolerance-profile-driven pitch/timing checks, octave policy, tied-note handling). Wired into `/performance/analyze` — alignment and assessment are now genuinely computed, not mocked.
- **Score persistence:** `ScoreStore`/`InMemoryScoreStore` (`score_store.py`) — not one of the plan's 7 modules, just the seam needed because `/score/import` and `/performance/analyze` are separate HTTP requests. `/performance/analyze` now looks up a real imported score by `score_id` (404 if it wasn't imported in this process); `score_id="mock-reference"` still gets the fixed demo score so the octave-off scenario works without importing first. In-process only — no durability across restarts, no multi-worker sharing, clearly flagged as a v1-only shortcut.
- **Test fixtures include two real (non-synthetic) MusicXML files:**
  - `bach_bwv140_7_bass_voice.musicxml` — a genuine Bach chorale bass line (music21's bundled public-domain corpus). Caught a real bug: `_extract_title` only checked music21's `.title` (work-title only), silently returning `None` for movement-title-only files — fixed with `.bestTitle`.
  - `simandl_etude1_mm1-3.musicxml` — the first fixture from an actual double-bass method book (Simandl's 30 Etudes, IMSLP, public domain), mm. 1-3 of Etude No. 1's Contrabass line. **Hand-transcribed, not OMR-verified** — two automated OMR attempts failed first (`oemer` needs model weights from a host outside the sandbox's network allowlist; a "free OMR" web tool turned out to return a hardcoded fake file, not a real conversion). Rhythm/tempo read with confidence; exact pitches are a best-effort visual read — flagged clearly in the fixture and its test.
- **53 backend tests, all passing.** Frontend: `eslint` clean, `tsc -b && vite build` succeeds.

## What's still mock

**`/performance/analyze`** (the original JSON-only endpoint) still uses `_mock_transcription()` — deliberately left unchanged (see above) rather than retrofitted. **`/performance/analyze_recording`** (new) is fully real end-to-end: real transcription + real alignment + real assessment. All 7 of the plan's modules now have real implementations; nothing in the core pipeline is mocked anymore except that one legacy endpoint kept for backward compatibility.

## First real end-to-end validation (scratch, not committed code)

Built a private reference score (`rabbath_etude1_lines1-2_UNVERIFIED.musicxml`, kept local/outside the repo — copyrighted Rabbath/Leduc publication, not public domain like Simandl) by having Naveen dictate note names from his own physical copy while Claude inferred rhythm from photos, corrected over several rounds — 62 notes, 55/55 clean structurally, `needs_review=True` throughout via `MUSICXML_PDF` source format. Ingests correctly at real sounding pitch (E1–A2 range, matching the instrument's low positions).

Ran it against Naveen's real recording (`IMG_0751.MOV`, "Étude No. 1" by Rabbath): extracted audio, pYIN pitch-tracked it, segmented into notes via `librosa.onset_detect` (rough scratch script, not yet a real `PitchTracker`), fed the result through the actual `OfflineDtwAligner` (`SUBSEQUENCE_DTW`) and `RuleBasedAssessor` — the first time real alignment/assessment code has run against a real recording rather than the mock transcription.

Result: the machinery works — real `NotePair`s formed, real mistake types generated (`extra_note`, `missed_note`, `wrong_pitch`, `timing_early/late`, `low_confidence`), and low-confidence suppression correctly held back verdicts on untrustworthy reads. Several matches were exact (e.g. ref note 10/11/18/19 all matched the performance note-for-note, same MIDI pitch). But onset detection only found 41 note events against the score's 62 — under-segmenting by roughly a third, likely because legato bowing doesn't produce sharp attack transients the way plucked/percussive onsets do — so only 2/62 score notes ended up confidently marked correct. This is squarely the transcription-quality gap the plan already flagged (module 4's frame-size/onset tradeoff), not a flaw in alignment or assessment.

**Next concrete step if pursued:** formalize this into a real `PitchTracker`/`OctaveCorrector` implementation in `transcription.py` (the scratch script is the starting point), tune onset detection for bowed-string attacks specifically, and get the additional known-wrong/skip-repeat takes the plan's eval strategy calls for.

## Environment facts worth re-checking each session, not treating as permanent

1. npm/pypi registry access has been open this session (confirmed with real `pip install`/`npm ci`, not just cached). Was blocked in an earlier session. Re-verify at the start of each session.
2. `github.com` is reachable; `api.github.com` is not (connection reset) — use raw `git` over HTTPS with a fine-grained PAT (needs **Contents** + **Workflows** read/write) rather than the repo-connector tool.
3. `/tmp` can carry stale ownership from a previous sandbox lifetime — check `stat -c '%U' <dir>` before reusing a path; use a fresh path if it doesn't match `whoami`.

## Blocked / needs you

1. **A proper labeled eval harness** (plan section 5) needs real recordings beyond the one take we have: a known-wrong take (deliberate errors) and a skip/repeat take, plus ideally more than one piece/tempo — the segmentation thresholds (`OVERSIZED_FACTOR`, `MIN_CONFIDENCE`) in `pyin_transcriber.py` are calibrated against exactly one real recording so far. Needs your bass back.
2. **Dictating the rest of the Rabbath etude** (lines 3-8, same process as lines 1-2: you read off note names, I infer rhythm from photos and you correct it) — only lines 1-2 (62 notes) exist as a reference score so far, kept local/private (copyright).
3. **A few real double-bass method-book excerpts** you've personally verified, to validate `simandl_etude1_mm1-3.musicxml`'s pitches against the actual source (or provide a better/longer excerpt) — the current fixture is honestly caveated as unverified.

## Next up (doesn't need audio, packages, or your input)

OMR engine bake-off notes for Phase 5 (Audiveris vs. oemer — plan section 6).
