"""Offline DTW aligner — v1 implementation of the `ScoreAligner` contract.

This is the concrete v1 (offline, monophonic, double-bass) implementation of the
abstract `ScoreAligner` from `score_align.py`. It maps performed (detected) notes
onto reference (score) notes and produces an `Alignment`. It does NOT classify
error severity — that is the assessment module's job (see the architecture doc's
explicit alignment<->assessment boundary). The aligner only decides *which
performed note corresponds to which score note (if any)*.

Algorithm choice
----------------
We roll our own dynamic-programming DTW table rather than calling `dtw-python`.
The reason is control over gap and segment semantics:

  * We need an *insertion/deletion* (gap) move whose cost is governed by
    `config.gap_cost_threshold`, so that a step the cost function says is "not
    really the same note" becomes a one-sided `NotePair` (missed score note or
    extra performed note) instead of a forced bad match. Off-the-shelf DTW only
    gives diagonal/horizontal/vertical *matching* steps; it has no first-class
    notion of "leave this note unmatched."
  * RESYNC needs to detect a contiguous run of high-cost steps and *segment*
    the alignment there, re-anchoring each side independently. That is far
    easier to express against a table we own than by post-processing a library's
    monotonic warping path.

The DP is a standard Needleman-Wunsch-style edit-distance table (the same shape
as DTW with explicit gap moves): three predecessors per cell — diagonal (match),
left (missed score note / deletion of a ref note), up (extra performed note /
insertion).

Local cost
----------
`_local_cost(perf_note, score_note, score, cfg)` combines:

  * Pitch cost: pitch-CLASS-aware distance. Circular distance on the 12-tone
    pitch class (0..6 semitones) plus a small octave penalty. This is
    intentional — an octave error costs *less* than an unrelated wrong note so
    that octave-misdetections (the dominant double-bass failure mode) still
    *align* rather than fall outside the gap threshold. Octave
    correction/classification is a downstream concern.
  * Timing cost: |perf.onset_s - score_onset_seconds| where the score onset in
    beats is converted to seconds via `score.tempo.bpm`. Normalised to a
    per-beat scale so the weights are interpretable.

Weighted by `config.pitch_cost_weight` / `config.timing_cost_weight`.

Gap handling
------------
A diagonal (match) step whose local cost exceeds `config.gap_cost_threshold` is
not allowed to be a match; the DP must instead take two gap moves (emit the
score note as missed and the performed note as extra). Gap moves cost
`gap_cost_threshold` each, so the DP naturally prefers a real match whenever one
exists below threshold and only "gaps out" genuinely dissimilar notes.

Strategies
----------
  * GLOBAL_DTW       — classic full monotonic alignment; both endpoints anchored
                       (every score note must be covered). Only legitimate when
                       the two lengths roughly match.
  * SUBSEQUENCE_DTW  — the performance matches a contiguous sub-span of the
                       score. Standard subsequence DTW: the start/end *boundary*
                       on the reference (score) axis is not penalised, so the
                       performance may begin and end anywhere in the score
                       without paying deletion cost for the un-played prefix /
                       suffix.
  * RESYNC           — run subsequence DTW, then scan the path for a contiguous
                       run of gap steps longer than `config.resync_window`
                       (a real skip or repeat). Segment there and re-run
                       subsequence DTW independently on each side, re-anchoring
                       rather than forcing one monotonic path through the gap.
                       Populates `segments` and `skip_repeat_spans`.

See `docs/ARCHITECTURE.md` -> `score_align.py` -> "Implementation notes" for the
honest accounting of what is solid vs. approximate (RESYNC repeat-detection in
particular is heuristic).
"""

from __future__ import annotations

from typing import Optional

from .score_align import (
    AlignConfig,
    AlignMode,
    Alignment,
    AlignSegment,
    AlignStrategy,
    NotePair,
    ScoreAligner,
    SkipRepeatSpan,
)
from .score_ingest import Score, ScoreNote
from .transcription import DetectedNote, Transcription

# DP move encodings used in the backtrace table.
_DIAG = 0  # match: consume one performed + one score note
_LEFT = 1  # deletion: consume one score note (missed), no performed note
_UP = 2  # insertion: consume one performed note (extra), no score note
_START = 3  # origin / free-start sentinel


class OfflineDtwAligner(ScoreAligner):
    """Offline DTW/edit-distance aligner for the v1 monophonic pipeline.

    Implements GLOBAL_DTW, SUBSEQUENCE_DTW, and RESYNC. Only supports
    `AlignMode.OFFLINE`; `align_incremental` is intentionally left to the ABC
    default (raises NotImplementedError) — that is the v2 online path.
    """

    # ------------------------------------------------------------------ public

    def supports_mode(self, mode: AlignMode) -> bool:
        return mode == AlignMode.OFFLINE

    def align(
        self,
        performance: Transcription,
        score: Score,
        config: Optional[AlignConfig] = None,
    ) -> Alignment:
        cfg = config if config is not None else AlignConfig()

        perf = performance.notes
        ref = score.notes

        if cfg.strategy == AlignStrategy.RESYNC:
            return self._align_resync(perf, ref, score, cfg)

        global_mode = cfg.strategy == AlignStrategy.GLOBAL_DTW
        pairs = self._dp_align(
            perf,
            ref,
            score,
            cfg,
            ref_offset=0,
            free_ref_boundaries=not global_mode,
        )
        segment = self._segment_from_pairs(pairs)
        return Alignment(
            mode=AlignMode.OFFLINE,
            strategy=cfg.strategy,
            pairs=pairs,
            segments=[segment] if segment is not None else [],
            skip_repeat_spans=[],
            is_partial=False,
        )

    # ------------------------------------------------------------- cost model

    def _local_cost(
        self, perf: DetectedNote, ref: ScoreNote, score: Score, cfg: AlignConfig
    ) -> float:
        """Combined pitch + timing local cost for matching one perf<->ref pair."""
        return (
            cfg.pitch_cost_weight * self._pitch_cost(perf, ref)
            + cfg.timing_cost_weight * self._timing_cost(perf, ref, score)
        )

    @staticmethod
    def _pitch_cost(perf: DetectedNote, ref: ScoreNote) -> float:
        """Pitch-class-aware distance.

        Circular pitch-class distance (0..6 semitones) dominates; an octave error
        adds only a small per-octave penalty so it stays well below the unrelated-
        wrong-note range and inside a reasonable gap threshold. Fine cents
        deviation is folded in at low weight (it matters far more to assessment
        than to alignment, but it helps break ties between equally plausible
        candidates).
        """
        pc_diff = abs(perf.pitch_class - ref.pitch_class) % 12
        pc_dist = min(pc_diff, 12 - pc_diff)  # 0..6

        octave_diff = abs(perf.midi - ref.midi) // 12
        octave_penalty = 0.5 * octave_diff  # cheap: octave errors still align

        cents_dist = abs(perf.cents_offset) / 100.0  # 0..~0.5

        return pc_dist + octave_penalty + cents_dist

    @staticmethod
    def _timing_cost(perf: DetectedNote, ref: ScoreNote, score: Score) -> float:
        """Absolute onset deviation in *beats* (tempo-normalised seconds)."""
        sec_per_beat = 60.0 / score.tempo.bpm
        ref_onset_s = ref.onset_beats * sec_per_beat
        delta_s = abs(perf.onset_s - ref_onset_s)
        return delta_s / sec_per_beat  # express in beats so weights are stable

    # ------------------------------------------------------------------- core

    def _dp_align(
        self,
        perf: list[DetectedNote],
        ref: list[ScoreNote],
        score: Score,
        cfg: AlignConfig,
        *,
        ref_offset: int,
        free_ref_boundaries: bool,
    ) -> list[NotePair]:
        """Needleman-Wunsch-style DP with explicit gap moves.

        Rows = performed notes (i), cols = reference notes (j).

        ``free_ref_boundaries`` toggles subsequence behaviour: when True the
        un-played prefix and suffix of the *reference* axis are free (no deletion
        cost), so the performance can match a contiguous sub-span of the score.
        When False (GLOBAL_DTW) both ends are anchored and every score note must
        be covered.

        ``ref_offset`` shifts emitted ``ref_index`` values so this routine can be
        reused on a slice of the score during RESYNC.
        """
        n = len(perf)
        m = len(ref)
        gap = cfg.gap_cost_threshold
        # Reward for a real match; must be in (0, gap) so a sub-threshold match
        # always beats splitting the two notes into a gap pair.
        match_reward = gap * 0.5

        if n == 0 and m == 0:
            return []
        if m == 0:
            # No reference span: every performed note is an extra note.
            return [
                NotePair(ref_index=None, performed_index=i, local_cost=gap)
                for i in range(n)
            ]
        if n == 0:
            # Nothing performed: every score note in this span is missed.
            return [
                NotePair(
                    ref_index=ref_offset + j, performed_index=None, local_cost=gap
                )
                for j in range(m)
            ]

        INF = float("inf")
        cost = [[INF] * (m + 1) for _ in range(n + 1)]
        back = [[_START] * (m + 1) for _ in range(n + 1)]

        cost[0][0] = 0.0
        # First column (j=0): only performed notes consumed so far -> every one is
        # an extra performed note (insertion) -> _UP.
        for i in range(1, n + 1):
            cost[i][0] = cost[i - 1][0] + gap
            back[i][0] = _UP
        # First row (i=0): only score notes consumed so far -> missed score notes
        # (deletion) -> _LEFT. Free under subsequence mode (the performance may
        # start mid-score for no cost).
        for j in range(1, m + 1):
            if free_ref_boundaries:
                cost[0][j] = 0.0
                # Free prefix: the performance may start mid-score. Mark these as
                # START sentinels so the backtrace stops here instead of emitting
                # the un-played prefix as a run of missed notes.
                back[0][j] = _START
            else:
                cost[0][j] = cost[0][j - 1] + gap
                back[0][j] = _LEFT

        for i in range(1, n + 1):
            for j in range(1, m + 1):
                lc = self._local_cost(perf[i - 1], ref[j - 1], score, cfg)
                # A match step above the gap threshold is disallowed: forcing it
                # would be a "forced bad match". Make it prohibitively costly so
                # the DP routes around it via two gap moves instead.
                if lc > gap:
                    diag = INF
                else:
                    # Match reward: a legitimate (sub-threshold) match is made
                    # strictly cheaper than gapping the two notes apart, so the
                    # optimal path threads through every real correspondence
                    # instead of an equal-cost degenerate route that drops a
                    # genuinely-matching note as a gap. `match_reward` < gap keeps
                    # a real match always preferable to a gap pair.
                    diag = cost[i - 1][j - 1] + lc - match_reward

                # (i, j-1): one more REF note consumed -> missed score note.
                left = cost[i][j - 1] + gap
                # (i-1, j): one more PERF note consumed -> extra performed note.
                up = cost[i - 1][j] + gap

                best = diag
                move = _DIAG
                if left < best:
                    best = left
                    move = _LEFT
                if up < best:
                    best = up
                    move = _UP
                cost[i][j] = best
                back[i][j] = move

        # Choose the end cell. Subsequence mode: the performance may end anywhere
        # in the score, so pick the min-cost cell along the last row (all
        # performed notes consumed) and treat the un-played suffix as free.
        end_i, end_j = n, m
        if free_ref_boundaries:
            best_j = m
            best_cost = cost[n][m]
            for j in range(0, m + 1):
                if cost[n][j] < best_cost:
                    best_cost = cost[n][j]
                    best_j = j
            end_j = best_j

        return self._backtrace(back, perf, ref, score, cfg, end_i, end_j, ref_offset)

    def _backtrace(
        self,
        back: list[list[int]],
        perf: list[DetectedNote],
        ref: list[ScoreNote],
        score: Score,
        cfg: AlignConfig,
        end_i: int,
        end_j: int,
        ref_offset: int,
    ) -> list[NotePair]:
        pairs: list[NotePair] = []
        i, j = end_i, end_j
        while i > 0 or j > 0:
            move = back[i][j]
            if move == _DIAG:
                lc = self._local_cost(perf[i - 1], ref[j - 1], score, cfg)
                pairs.append(
                    NotePair(
                        ref_index=ref_offset + (j - 1),
                        performed_index=i - 1,
                        local_cost=lc,
                    )
                )
                i -= 1
                j -= 1
            elif move == _LEFT:
                # Missed score note: consumed ref j-1 with no performed match.
                pairs.append(
                    NotePair(
                        ref_index=ref_offset + (j - 1),
                        performed_index=None,
                        local_cost=cfg.gap_cost_threshold,
                    )
                )
                j -= 1
            elif move == _UP:
                # Extra performed note: consumed perf i-1 with no score match.
                pairs.append(
                    NotePair(
                        ref_index=None,
                        performed_index=i - 1,
                        local_cost=cfg.gap_cost_threshold,
                    )
                )
                i -= 1
            else:  # _START sentinel reached on a free boundary
                break
        pairs.reverse()
        return pairs

    # --------------------------------------------------------------- segments

    @staticmethod
    def _segment_from_pairs(pairs: list[NotePair]) -> Optional[AlignSegment]:
        """Build a single AlignSegment spanning the matched reference range."""
        ref_indices = [p.ref_index for p in pairs if p.ref_index is not None]
        if not ref_indices:
            return None
        return AlignSegment(
            pairs=pairs, ref_start=min(ref_indices), ref_end=max(ref_indices)
        )

    # ----------------------------------------------------------------- resync

    # A matched pair whose local cost exceeds this fraction of the gap threshold
    # is treated as a "bad" step for break detection — the monotonic path is
    # being forced through a poor region even though it stayed under the hard gap
    # ceiling. This is what lets RESYNC spot a skip whose forced match is merely
    # mediocre rather than impossible.
    _BADNESS_FRACTION = 0.5

    def _align_resync(
        self,
        perf: list[DetectedNote],
        ref: list[ScoreNote],
        score: Score,
        cfg: AlignConfig,
    ) -> Alignment:
        """RESYNC: detect a real skip/repeat, segment, and re-anchor each side.

        Plain (subsequence) DTW forces one monotonic path. When the performer
        skips or repeats a section, that single path either deletes a long
        interior span of the score (a skip) or cannot place a run of replayed
        notes (a repeat) — or, worse, force-matches notes at mediocre cost. We:

          1. Run a GLOBAL (anchored) DTW first pass so a skip/repeat
             surfaces as a long run of one-sided steps.
          2. Find the first contiguous run of *bad* steps longer than
             `config.resync_window`. A step is bad if it is a gap pair OR a match
             whose local cost is above `gap * _BADNESS_FRACTION`.
          3. Split the PERFORMANCE at that run (by performed index) into a
             "before" slice and an "after" slice, then align each slice against
             the whole score independently with subsequence DTW. Re-anchoring per
             slice is exactly what lets the "after" slice jump forward (skip) or
             backward (repeat) instead of being dragged along one path.
          4. Emit one AlignSegment per re-anchored slice and a SkipRepeatSpan
             classifying the break (see `_classify_span`).
        """
        # First pass uses GLOBAL (anchored) boundaries on purpose: a skip then
        # shows up as a long interior run of missed score notes, and a repeat as
        # a run of extra performed notes — both of which `_find_break` can catch.
        # (A subsequence first pass would silently absorb a skipped prefix/suffix
        # as a free boundary and hide the break.) The per-slice RE-alignment below
        # still uses subsequence so each slice can re-anchor freely.
        first_pass = self._dp_align(
            perf, ref, score, cfg, ref_offset=0, free_ref_boundaries=False
        )

        break_run = self._find_break(first_pass, cfg)
        if break_run is None:
            seg = self._segment_from_pairs(first_pass)
            return Alignment(
                mode=AlignMode.OFFLINE,
                strategy=AlignStrategy.RESYNC,
                pairs=first_pass,
                segments=[seg] if seg is not None else [],
                skip_repeat_spans=[],
                is_partial=False,
            )

        run_start, run_end = break_run  # inclusive indices into first_pass

        # Find the performed-note index at which the "after" slice resumes. We
        # split the PERFORMANCE so each slice can re-anchor independently; this is
        # robust for both skip and repeat (unlike splitting the score, which a
        # repeat would double-cover).
        perf_resume = self._first_perf_index(first_pass[run_end + 1 :])
        if perf_resume is None:
            # The bad run runs to the end: split right after the last good perf
            # match before the run.
            last_good_perf = self._last_perf_index(first_pass[:run_start])
            perf_resume = (last_good_perf + 1) if last_good_perf is not None else len(perf)

        left_perf = perf[:perf_resume]
        right_perf = perf[perf_resume:]

        # Each slice is aligned against the WHOLE score (subsequence), so it
        # re-anchors wherever it truly fits.
        left_pairs = self._dp_align(
            left_perf, ref, score, cfg, ref_offset=0, free_ref_boundaries=True
        )
        right_pairs = self._dp_align(
            right_perf, ref, score, cfg, ref_offset=0, free_ref_boundaries=True
        )
        # Shift performed indices on the right slice back to global numbering.
        for p in right_pairs:
            if p.performed_index is not None:
                p.performed_index += perf_resume

        left_anchor = self._max_ref_index(
            [p for p in left_pairs if p.performed_index is not None]
        )
        right_anchor = self._min_ref_index(
            [p for p in right_pairs if p.performed_index is not None]
        )

        span = self._classify_span(left_pairs, right_pairs, left_anchor, right_anchor)

        all_pairs = left_pairs + right_pairs
        segments = []
        left_seg = self._segment_from_pairs(left_pairs)
        right_seg = self._segment_from_pairs(right_pairs)
        if left_seg is not None:
            segments.append(left_seg)
        if right_seg is not None:
            segments.append(right_seg)

        return Alignment(
            mode=AlignMode.OFFLINE,
            strategy=AlignStrategy.RESYNC,
            pairs=all_pairs,
            segments=segments,
            skip_repeat_spans=[span] if span is not None else [],
            is_partial=False,
        )

    def _find_break(
        self, pairs: list[NotePair], cfg: AlignConfig
    ) -> Optional[tuple[int, int]]:
        """First contiguous run of *bad* steps longer than `config.resync_window`.

        A step is bad if it is a gap (one-sided) pair, or a match whose local cost
        exceeds `gap * _BADNESS_FRACTION` (a forced mediocre match). Returns
        (start_idx, end_idx) inclusive into `pairs`, or None.
        """
        badness = cfg.gap_cost_threshold * self._BADNESS_FRACTION
        run_start = None
        for idx, p in enumerate(pairs):
            is_gap = p.ref_index is None or p.performed_index is None
            is_bad_match = (
                not is_gap and p.local_cost > badness
            )
            bad = is_gap or is_bad_match
            if bad:
                if run_start is None:
                    run_start = idx
            else:
                if run_start is not None:
                    if idx - run_start > cfg.resync_window:
                        return (run_start, idx - 1)
                    run_start = None
        if run_start is not None and len(pairs) - run_start > cfg.resync_window:
            return (run_start, len(pairs) - 1)
        return None

    def _classify_span(
        self,
        left_pairs: list[NotePair],
        right_pairs: list[NotePair],
        left_anchor: Optional[int],
        right_anchor: Optional[int],
    ) -> Optional[SkipRepeatSpan]:
        """Classify the break as skipped vs. repeated from the re-anchor points.

        Honest about its limits (see ARCHITECTURE.md):

          * If the "after" slice re-anchors FORWARD of where the "before" slice
            left off, leaving an un-covered interior span of the score, that span
            was SKIPPED. We report it as the gap between the two anchors.
          * If the "after" slice re-anchors at or BEFORE the "before" slice's
            last matched score note, the performer replayed an earlier region —
            a REPEAT. We report the replayed reference span (right_anchor..
            left_anchor).

        Repeat detection is the weaker case: re-anchoring backward is a strong
        signal, but proving the replay re-covers a *specific* earlier span
        exactly would need a dedicated second pass. The reported "repeated" span
        is therefore best-effort.
        """
        if left_anchor is None or right_anchor is None:
            return None

        if right_anchor > left_anchor + 1:
            # Gap of un-played score notes between the two anchored runs.
            return SkipRepeatSpan(
                kind="skipped",
                ref_start=left_anchor + 1,
                ref_end=right_anchor - 1,
            )
        if right_anchor <= left_anchor:
            # The after-slice jumped backward: a replayed (repeated) region.
            return SkipRepeatSpan(
                kind="repeated",
                ref_start=right_anchor,
                ref_end=left_anchor,
            )
        return None

    # --------------------------------------------------- small index helpers

    @staticmethod
    def _max_ref_index(pairs: list[NotePair]) -> Optional[int]:
        vals = [p.ref_index for p in pairs if p.ref_index is not None]
        return max(vals) if vals else None

    @staticmethod
    def _min_ref_index(pairs: list[NotePair]) -> Optional[int]:
        vals = [p.ref_index for p in pairs if p.ref_index is not None]
        return min(vals) if vals else None

    @staticmethod
    def _first_perf_index(pairs: list[NotePair]) -> Optional[int]:
        """First performed index in path order (the resume point of a slice)."""
        for p in pairs:
            if p.performed_index is not None:
                return p.performed_index
        return None

    @staticmethod
    def _last_perf_index(pairs: list[NotePair]) -> Optional[int]:
        """Last performed index in path order."""
        for p in reversed(pairs):
            if p.performed_index is not None:
                return p.performed_index
        return None
