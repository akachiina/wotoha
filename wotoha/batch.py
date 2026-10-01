"""Batch utilities: interactive selection and multi-manga downloads."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

log = logging.getLogger(__name__)


def parse_pick(spec: str, count: int) -> list[int]:
    """Converts "1,3-5" into valid 0-based indexes."""
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if lo < 1 or hi < lo:
                raise ValueError(f"invalid range: '{part}'")
            chosen.update(range(lo - 1, hi))
        else:
            chosen.add(int(part) - 1)
    result = sorted(i for i in chosen if 0 <= i < count)
    if not result:
        raise ValueError(f"no valid index in '{spec}' (1-{count})")
    return result


def pick_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Asks the user to pick search results (--input-pos style)."""
    if not results:
        return []
    print("\nResults:")
    for i, m in enumerate(results, start=1):
        status = m.get("status") or "?"
        print(f"  {i}. [{m.get('sourceId', '?')}] {m.get('title', '?')} ({status})")
    try:
        spec = input("\nPick (e.g. 1,3-5; empty cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []
    if not spec:
        return []
    idxs = parse_pick(spec, len(results))
    return [results[i] for i in idxs]
