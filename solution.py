"""Submission entry point: ``build()`` -> KnowledgeSystem (ingest / answer / conflicts).

The harness contract types are used when the harness is importable; otherwise structurally
identical local dataclasses are used (the harness converts any same-shaped object), so this folder
runs standalone and never depends on files outside it.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:  # pragma: no cover - exercised only inside the official harness
    from contract import Answer, Conflict, Source  # type: ignore
except Exception:  # noqa: BLE001 - any import problem falls back to local types
    @dataclass
    class Source:  # type: ignore[no-redef]
        url: str
        snapshot_id: Optional[str] = None
        quote: Optional[str] = None

    @dataclass
    class Answer:  # type: ignore[no-redef]
        text: str
        sources: list = field(default_factory=list)
        confident: bool = True
        conflict_note: Optional[str] = None

    @dataclass
    class Conflict:  # type: ignore[no-redef]
        topic: str
        values: list
        sources: list
        resolution: Optional[str] = None
        reason: Optional[str] = None

from tks.conflicts import find_conflicts  # noqa: E402
from tks.engine import REFUSAL_TEXT, AnswerEngine  # noqa: E402
from tks.store import KnowledgeStore  # noqa: E402

MAX_SOURCES = 4
QUOTE_CHARS = 240


class TemporalKnowledgeSystem:
    """Deterministic temporal evidence engine over versioned website snapshots."""

    def __init__(self) -> None:
        self.store = KnowledgeStore()
        self.engine = AnswerEngine(self.store)
        self._lock = threading.Lock()
        self._answer_cache: dict[tuple[str, Optional[str]], Answer] = {}
        self._conflicts: Optional[list] = None

    # ------------------------------------------------------------------ contract
    def ingest(self, snapshot) -> None:
        with self._lock:
            self.store.ingest(snapshot.snapshot_id, snapshot.taken_on, snapshot.pages)
            self.engine = AnswerEngine(self.store)
            self._answer_cache.clear()
            self._conflicts = None

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        key = ((question or "").strip(), as_of)
        with self._lock:
            if key in self._answer_cache:
                return self._answer_cache[key]
            try:
                ev, k = self.engine.answer(question or "", as_of)
            except Exception as exc:  # never crash the caller; abstain instead
                return Answer(text=REFUSAL_TEXT, sources=[], confident=False,
                              conflict_note=f"internal error: {type(exc).__name__}")
            sources = []
            seen = set()
            for b in ev.blocks:
                if b.url in seen:
                    continue
                seen.add(b.url)
                sid = self._snapshot_id_for(b, k)
                sources.append(Source(url=b.url, snapshot_id=sid, quote=b.text[:QUOTE_CHARS]))
                if len(sources) >= MAX_SOURCES:
                    break
            ans = Answer(text=ev.text, sources=sources, confident=ev.confident,
                         conflict_note=ev.note)
            self._answer_cache[key] = ans
            return ans

    def conflicts(self) -> list:
        with self._lock:
            if self._conflicts is None:
                out = []
                for c in find_conflicts(self.store):
                    srcs = [Source(url=b.url, snapshot_id=self.store.snapshots[b.last_snapshot].snapshot_id,
                                   quote=b.text[:QUOTE_CHARS]) for b in c.blocks]
                    out.append(Conflict(topic=c.topic, values=c.values, sources=srcs,
                                        resolution=c.resolution, reason=c.reason))
                self._conflicts = out
            return list(self._conflicts)

    # ------------------------------------------------------------------ helpers
    def _snapshot_id_for(self, block, k: Optional[int]) -> Optional[str]:
        if k is None:
            return None
        mask = block.observed & ((1 << (k + 1)) - 1)
        idx = mask.bit_length() - 1 if mask else block.last_snapshot
        return self.store.snapshots[idx].snapshot_id if idx >= 0 else None


def build() -> TemporalKnowledgeSystem:
    return TemporalKnowledgeSystem()
