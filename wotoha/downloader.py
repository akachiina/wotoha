"""Chapter/page download pipeline via Suwayomi-Server.

Progress/retry concepts inspired by mangadex-downloader
(REF/mangadex-downloader/mangadex_downloader/downloader.py).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import SuwayomiClient
from .config import block_sigint
from .errors import ChapterNotFound, DownloadFailed, MangaDlError

log = logging.getLogger(__name__)

SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str, maxlen: int = 120) -> str:
    """Sanitizes a name for filesystem use."""
    cleaned = SAFE_NAME_RE.sub("_", name).strip(" .")
    return (cleaned or "unnamed")[:maxlen]


def parse_page_range(spec: str | None, total: int) -> list[int]:
    """Converts --pages into 0-based indexes (mangadex-downloader semantics).

    - "5"      → page 5 only
    - "1-10"   → pages 1 through 10
    - "5-"     → from page 5 to the end
    - "3--"    → the last 3 pages
    - "-5"     → everything but the last 5 (negative end = from the end)
    """
    if not spec or not spec.strip():
        return list(range(total))
    s = re.sub(r"\s", "", spec)

    m = re.fullmatch(r"(\d+)--", s)  # last N
    if m:
        n = int(m.group(1))
        return list(range(max(0, total - n), total))

    m = re.fullmatch(r"-(\d+)", s)  # -N: excludes the last N
    if m:
        n = int(m.group(1))
        end = max(0, total - n)
        return list(range(0, end))

    m = re.fullmatch(r"(\d+)-", s)  # N-: from N to the end
    if m:
        start = int(m.group(1))
        if not 1 <= start <= total:
            raise ChapterNotFound(f"page {start} out of range (1-{total})")
        return list(range(start - 1, total))

    m = re.fullmatch(r"(\d+)-(\d+)", s)  # range
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start < 1 or end < start:
            raise ChapterNotFound(f"invalid page range: '{spec}'")
        return list(range(start - 1, min(end, total)))

    m = re.fullmatch(r"(\d+)", s)  # single page
    if m:
        page = int(m.group(1))
        if not 1 <= page <= total:
            raise ChapterNotFound(f"page {page} out of range (1-{total})")
        return [page - 1]

    raise ChapterNotFound(f"invalid page specification: '{spec}'")


@dataclass
class ChapterRef:
    """Reference to a Suwayomi chapter."""

    id: int
    index: int  # sourceOrder do Suwayomi (1-based)
    name: str
    chapter_number: float
    scanlator: str | None
    page_count: int
    volume_number: float | None = None  # Suwayomi does not expose volume

    @property
    def is_oneshot(self) -> bool:
        import re as _re

        return self.chapter_number in (None, 0) and bool(
            _re.search(r"one[\s-]?shot", self.name or "", _re.IGNORECASE)
        )


class ChapterDownloader:
    """Downloads a manga's chapters to disk."""

    def __init__(self, client: SuwayomiClient, workers: int = 4, retries: int = 3) -> None:
        self.client = client
        self.workers = workers
        self.retries = retries

    # ------------------------------------------------------------------ #

    def list_chapters(self, manga_id: int, no_oneshot: bool = False) -> list[ChapterRef]:
        """Chapter list (ordered from newest to oldest,
        as Suwayomi returns them)."""
        chapters: list[dict[str, Any]] = self.client.get_chapters(manga_id)
        refs = [
            ChapterRef(
                id=c["id"],
                index=c["index"],
                name=c.get("name") or f"Chapter {c.get('chapterNumber')}",
                chapter_number=float(c.get("chapterNumber") or 0),
                scanlator=c.get("scanlator"),
                page_count=int(c.get("pageCount") or -1),
            )
            for c in chapters
        ]
        if no_oneshot:
            refs = [c for c in refs if not c.is_oneshot]
        return refs

    def select_chapters(
        self,
        chapters: list[ChapterRef],
        spec: str | None = None,
        start_chapter: float | None = None,
        end_chapter: float | None = None,
    ) -> list[ChapterRef]:
        """Selects chapters per the user specification.

        - None or "all": all
        - "latest": only the most recent chapter
        - "1-10,15,20-23": ranges by **chapter number** (not index)
        - start_chapter/end_chapter: additional numeric cut
        """
        if not chapters:
            return []
        items = chapters
        if spec and spec.strip().lower() not in ("all", "tudo"):
            items = self._select_by_spec(chapters, spec)
        if start_chapter is not None or end_chapter is not None:
            lo = start_chapter if start_chapter is not None else float("-inf")
            hi = end_chapter if end_chapter is not None else float("inf")
            items = [
                c for c in items
                if c.chapter_number is not None and lo <= c.chapter_number <= hi
            ]
        return items

    def _select_by_spec(self, chapters: list[ChapterRef], spec: str) -> list[ChapterRef]:
        spec = spec.strip().lower()
        if spec in ("latest", "ultimo", "último"):
            return chapters[:1]

        by_number = {c.chapter_number: c for c in chapters}
        selected: dict[float, ChapterRef] = {}
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_s, _, end_s = part.partition("-")
                try:
                    start, end = float(start_s), float(end_s)
                except ValueError as e:
                    raise MangaDlError(f"Invalid range: '{part}'") from e
                for num in by_number:
                    if start <= num <= end:
                        selected[num] = by_number[num]
            else:
                try:
                    num = float(part)
                except ValueError as e:
                    raise MangaDlError(f"Invalid chapter number: '{part}'") from e
                if num in by_number:
                    selected[num] = by_number[num]
                else:
                    log.warning("Chapter %s not found", part)
        # keeps the original order (newest first)
        return [c for c in chapters if c.id in {s.id for s in selected.values()}]

    # ------------------------------------------------------------------ #

    def _fetch_page(
        self,
        manga_id: int,
        chapter_index: int,
        page_index: int,
        stop: threading.Event | None = None,
    ) -> bytes:
        """Downloads a page with retries and exponential backoff.

        4xx statuses are permanent (don't waste time retrying); `stop`
        makes the backoff sleep exit immediately on Ctrl+C."""
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            if stop is not None and stop.is_set():
                break
            try:
                data = self.client.get_page_image(manga_id, chapter_index, page_index)
                if not data:
                    raise DownloadFailed("empty response")
                return data
            except Exception as e:  # noqa: BLE001 - generic retry
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500:
                    break
                if attempt >= self.retries:
                    break
                delay = min(2 ** attempt, 10)
                log.warning(
                    "Page %d failed (attempt %d/%d): %s. Retrying in %ds",
                    page_index, attempt, self.retries, e, delay,
                )
                # interruptible backoff: exits as soon as Ctrl+C is seen
                if stop is not None and stop.wait(delay):
                    break
        raise DownloadFailed(
            f"Page {page_index} failed after {self.retries} attempts"
        ) from last_err

    def download_chapter(
        self,
        manga: dict[str, Any],
        chapter: ChapterRef,
        dest: Path,
        pages_spec: str | None = None,
    ) -> Path:
        """Downloads a chapter's pages to `dest`.

        Suwayomi flow: the first call to /page/0 makes the server fetch the
        page list on the site (PageTable populate) and update the chapter's pageCount.
        For that reason we "warm up" with page 0 before parallelizing.
        """
        dest.mkdir(parents=True, exist_ok=True)
        marker = dest / ".complete"
        if marker.is_file():
            log.info("Chapter already downloaded, skipping: %s", dest.name)
            return dest

        log.info("Downloading '%s'", chapter.name)

        # 1) GET /chapter/{index}: makes the server populate pageCount and the page list
        info = self.client.get_chapter(manga["id"], chapter.index)
        page_count = int(info.get("pageCount") or 0)
        if page_count < 1:
            raise DownloadFailed(
                f"invalid pageCount ({page_count}) for chapter '{chapter.name}'"
            )

        # 2) indexes to download (honors --pages)
        indices = parse_page_range(pages_spec, page_count)
        log.info("  %d page(s)%s", len(indices), f" of {page_count}" if len(indices) < page_count else "")

        # warm-up: populates the page list on the server (always page 0)
        if 0 not in indices:
            self._fetch_page(manga["id"], chapter.index, 0)

        stop = threading.Event()

        def fetch(i: int) -> tuple[int, bytes]:
            return i, self._fetch_page(manga["id"], chapter.index, i, stop)

        with ThreadPoolExecutor(
            max_workers=self.workers, initializer=block_sigint
        ) as pool:
            futures = {pool.submit(fetch, i): i for i in indices}
            done = 0
            try:
                for fut in as_completed(futures):
                    page_index, data = fut.result()
                    ext = self._ext_from(data)
                    (dest / f"{page_index + 1:04d}{ext}").write_bytes(data)
                    done += 1
                    log.info("  [%d/%d] ok", done, len(indices))
            except KeyboardInterrupt:
                # tell the workers to stop sleeping/retrying and drop queued
                # pages, so the executor shuts down without hanging
                stop.set()
                for f in futures:
                    f.cancel()
                raise

        marker.write_text(str(len(indices)))
        return dest

    @staticmethod
    def _ext_from(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:3] == b"GIF":
            return ".gif"
        if data[:2] == b"\xff\xd8":
            return ".jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data[:12] == b"\x00\x00\x00\x18ftypavif" or data[:4] == b"\x00\x00\x00 ftyp":
            return ".avif"
        return ".jpg"
