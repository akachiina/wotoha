"""Batch utilities: interactive selection and multi-manga downloads."""

from __future__ import annotations

import logging
import sys
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

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
            try:
                lo, hi = int(a), int(b)
            except ValueError as e:
                raise ValueError(f"invalid range: '{part}'") from e
            if lo < 1 or hi < lo:
                raise ValueError(f"invalid range: '{part}'")
            chosen.update(range(lo - 1, hi))
        else:
            try:
                chosen.add(int(part) - 1)
            except ValueError as e:
                raise ValueError(f"invalid index: '{part}'") from e
    result = sorted(i for i in chosen if 0 <= i < count)
    if not result:
        raise ValueError(f"no valid index in '{spec}' (1-{count})")
    return result


def pick_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Asks the user to pick search results (companion to the numbered table).

    The caller prints the numbered results table first; this only prompts,
    looping on invalid input instead of crashing. Empty input cancels.
    """
    if not results:
        return []
    console = Console()
    while True:
        try:
            spec = Prompt.ask("\nPick (e.g. 1,3-5; empty cancels)")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return []
        spec = spec.strip()
        if not spec:
            return []
        try:
            idxs = parse_pick(spec, len(results))
        except ValueError as e:
            console.print(f"[yellow]Invalid selection: {e}. Use e.g. 1,3-5.[/yellow]")
            continue
        return [results[i] for i in idxs]
