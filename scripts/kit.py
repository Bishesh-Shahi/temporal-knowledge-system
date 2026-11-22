"""Load snapshot folders in the harness layout (manifest.json + pages/*.md) without the harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_snapshots(root: Path) -> list[SimpleNamespace]:
    snaps = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()):
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        pages = [SimpleNamespace(url=p["url"], title=p.get("title", ""),
                                 markdown=(d / p["file"]).read_text(encoding="utf-8"),
                                 fetched_at=p.get("fetched_at", m["taken_on"])) for p in m["pages"]]
        snaps.append(SimpleNamespace(snapshot_id=m["snapshot_id"], taken_on=m["taken_on"], pages=pages))
    snaps.sort(key=lambda s: s.taken_on)
    return snaps
