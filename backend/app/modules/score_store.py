"""Score persistence — a pragmatic stand-in, not one of the plan's 7 modules.

`/score/import` and `/performance/analyze` are separate HTTP requests, so
something has to hold a `Score` between them so the latter can look one up
by `score_id`. That's all this is: a narrow, swappable storage seam so
`main.py` doesn't hardcode "how scores are stored" any more than it
hardcodes "how scores are ingested" or "how alignment works".

v1 (`InMemoryScoreStore`) is a plain in-process dict — fine for a skeleton
API with one worker process and no durability requirement, and explicitly
NOT fine once there's more than one worker or the process restarts. Swap in
a real backing store (Postgres, SQLite, Redis) by implementing `ScoreStore`
against it; nothing else in `main.py` needs to change, same swap-without-
touching-callers pattern as every other contract in this codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .score_ingest import Score


class ScoreStore(ABC):
    """Contract for persisting/looking up canonical `Score`s by id."""

    @abstractmethod
    def save(self, score: Score) -> None:
        """Persist `score`, overwriting any existing score with the same id."""

    @abstractmethod
    def get(self, score_id: str) -> Optional[Score]:
        """Look up a previously-saved score, or None if `score_id` is unknown."""


class InMemoryScoreStore(ScoreStore):
    """v1 ScoreStore: an in-process dict. No durability, no multi-worker
    sharing — scores vanish on process restart and aren't visible across
    separate `uvicorn` worker processes. Fine for a single-process skeleton
    API and for tests; replace before this app runs with more than one
    worker or needs to survive a restart.
    """

    def __init__(self) -> None:
        self._scores: dict[str, Score] = {}

    def save(self, score: Score) -> None:
        self._scores[score.score_id] = score

    def get(self, score_id: str) -> Optional[Score]:
        return self._scores.get(score_id)
