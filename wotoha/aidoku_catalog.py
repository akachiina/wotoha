"""Aidoku sources catalog: unified API over the booted WASM runtimes."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import models
from .aidoku_runtime import AidokuRuntime
from .aidoku_store import sources_dir

log = logging.getLogger(__name__)

# ids de manga em buscas ficam "sourceId::mangaKey" p/ resolver depois
COMPOSITE_RE = re.compile(r"^([a-z0-9.]+)::(.+)$", re.IGNORECASE)


def make_composite_id(source_id: str, key: str) -> str:
    return f"{source_id}::{key}"


def split_composite_id(composite: str) -> tuple[str, str]:
    m = COMPOSITE_RE.match(composite)
    if not m:
        raise ValueError(
            f"invalid id: {composite!r} (expected sourceId::mangaKey from a search)",
        )
    return m.group(1), m.group(2)


class AidokuCatalog:
    """Boots runtimes on demand and exposes search/manga/chapters/pages."""

    def __init__(self) -> None:
        self._runtimes: dict[str, AidokuRuntime] = {}

    def runtime_for(self, source_id: str) -> AidokuRuntime:
        if source_id in self._runtimes:
            return self._runtimes[source_id]
        aix = sources_dir() / f"{source_id}.aix"
        if not aix.is_file():
            raise FileNotFoundError(
                f"source '{source_id}' not installed; run: wotoha sources install {source_id}"
            )
        rt = AidokuRuntime(aix)
        self._runtimes[source_id] = rt
        return rt

    # ------------------------------------------------------------------ #
    # operations
    # ------------------------------------------------------------------ #

    def search(self, source_id: str, query: str, page: int = 1) -> dict[str, Any]:
        rt = self.runtime_for(source_id)
        result = rt.get_search_manga_list(query, page)
        return {
            "mangaList": [self._entry(source_id, m) for m in result.entries],
            "hasNextPage": result.has_next_page,
        }

    def manga_details(self, composite_id: str) -> dict[str, Any]:
        source_id, key = split_composite_id(composite_id)
        rt = self.runtime_for(source_id)
        stub = models.Manga(key=key, title="")
        full = rt.get_manga_update(stub, needs_details=True, needs_chapters=True)
        d = full.to_dict()
        d["id"] = composite_id
        d["sourceId"] = source_id
        d["chapters"] = [c.to_dict() for c in (full.chapters or [])]
        for i, c in enumerate(d["chapters"]):
            c["index"] = i
            c["id"] = f"{composite_id}::{c['key']}"
        return d

    def chapter_pages(self, composite_id: str, chapter_key: str) -> list[str]:
        source_id, key = split_composite_id(composite_id)
        rt = self.runtime_for(source_id)
        # fetch the full manga metadata (with chapters)
        full = rt.get_manga_update(models.Manga(key=key, title=""))
        chapter = next((c for c in full.chapters or [] if c.key == chapter_key), None)
        if chapter is None:
            raise ValueError(f"chapter '{chapter_key}' not found")
        pages = rt.get_page_list(full, chapter)
        urls = [p.image_url() for p in pages]
        return [u for u in urls if u]

    def fetch_image(self, source_id: str, url: str) -> bytes:
        return self.runtime_for(source_id).fetch_image(url)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _entry(source_id: str, m: models.Manga) -> dict[str, Any]:
        d = m.to_dict()
        d["id"] = make_composite_id(source_id, m.key)
        d["sourceId"] = source_id
        return d
