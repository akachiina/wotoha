"""Native manga downloads from Aidoku sources (no Suwayomi)."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .aidoku_catalog import AidokuCatalog
from .config import block_sigint, config
from .downloader import parse_page_range, safe_name
from .errors import ChapterNotFound, MangaDlError

log = logging.getLogger(__name__)

ONESHOT_RE = re.compile(r"one[\s-]?shot", re.IGNORECASE)


@dataclass
class NativeChapter:
    """Aidoku chapter adapter for the output formats."""

    index: int
    key: str
    name: str
    chapter_number: float | None
    composite_id: str
    volume_number: float | None = None
    title: str | None = None
    date_uploaded: int | None = None
    scanlators: list[str] | None = None
    # keys of other releases (scanlation groups) of the same chapter, tried
    # in order when the primary release can't be downloaded completely
    fallbacks: list[str] = field(default_factory=list)

    @property
    def id(self) -> int:  # compat with the output formats
        return self.index

    @property
    def is_oneshot(self) -> bool:
        return self.chapter_number is None and bool(ONESHOT_RE.search(self.name or ""))


class NativeDownloader:
    """Downloads chapters via AidokuCatalog using the same folder layout."""

    def __init__(
        self,
        catalog: AidokuCatalog | None = None,
        workers: int = 4,
        retries: int = 3,
    ) -> None:
        self.catalog = catalog or AidokuCatalog()
        self.workers = workers
        self.retries = retries

    # ------------------------------------------------------------------ #
    # listing / selection

    def list_chapters(self, composite_id: str, no_oneshot: bool = False) -> list[NativeChapter]:
        """Lists chapters, deduplicating multiple releases of the same chapter
        (multi-group sources like MangaDex return one entry per group). The
        first release is primary; the others become fallbacks that
        download_chapter tries when the primary one is incomplete."""
        details = self.catalog.manga_details(composite_id)
        chapters = details.get("chapters") or []
        result: list[NativeChapter] = []
        seen: dict[tuple, NativeChapter] = {}
        for i, c in enumerate(chapters):
            title = c.get("title")
            num = c.get("chapterNumber")
            name = f"Chapter {num:g}" if num is not None else (title or f"Chapter {i + 1}")
            if title and num is not None and title != name:
                name = f"{name} - {title}"
            group = ("num", float(num)) if num is not None else ("title", title or f"Chapter {i + 1}")
            primary = seen.get(group)
            if primary is not None:
                primary.fallbacks.append(c["key"])
                continue
            ch = NativeChapter(
                index=len(result),
                key=c["key"],
                name=name,
                chapter_number=num,
                composite_id=composite_id,
                volume_number=c.get("volumeNumber"),
                title=title,
                date_uploaded=c.get("dateUploaded"),
                scanlators=c.get("scanlators"),
            )
            if no_oneshot and ch.is_oneshot:
                continue
            seen[group] = ch
            result.append(ch)
        return result

    def select_chapters(
        self,
        chapters: list[NativeChapter],
        spec: str | None,
        start_chapter: float | None = None,
        end_chapter: float | None = None,
    ) -> list[NativeChapter]:
        if not chapters:
            return []
        items = chapters
        if spec and spec.strip().lower() not in ("all", "tudo"):
            items = self._select_by_spec(chapters, spec)
        # numeric start/end (from mangadex-downloader)
        if start_chapter is not None or end_chapter is not None:
            lo = start_chapter if start_chapter is not None else float("-inf")
            hi = end_chapter if end_chapter is not None else float("inf")
            items = [
                c
                for c in items
                if c.chapter_number is not None and lo <= c.chapter_number <= hi
            ]
        return items

    def _select_by_spec(self, chapters: list[NativeChapter], spec: str) -> list[NativeChapter]:
        s = spec.strip().lower()
        if s in ("latest", "ultimo", "último"):
            return chapters[:1]

        by_num = {}
        for c in chapters:
            if c.chapter_number is not None:
                by_num[float(c.chapter_number)] = c
        selected: dict[str, NativeChapter] = {}
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_s, _, end_s = part.partition("-")
                try:
                    lo, hi = float(start_s), float(end_s)
                except ValueError as e:
                    raise ChapterNotFound(f"invalid range: '{part}'") from e
                for num, c in by_num.items():
                    if lo <= num <= hi:
                        selected[c.key] = c
            else:
                try:
                    num = float(part)
                except ValueError as e:
                    raise ChapterNotFound(f"invalid chapter: '{part}'") from e
                c = by_num.get(num)
                if c:
                    selected[c.key] = c
                else:
                    log.warning("chapter %s not found", part)
        return [c for c in chapters if c.key in selected]

    def group_volumes(self, chapters: list[NativeChapter]) -> list[tuple[float | None, list[NativeChapter]]]:
        """Groups chapters by volume, preserving discovery order."""
        groups: dict[float | None, list[NativeChapter]] = {}
        order: list[float | None] = []
        for c in chapters:
            vol = c.volume_number if c.volume_number is not None else None
            if vol not in groups:
                groups[vol] = []
                order.append(vol)
            groups[vol].append(c)
        # None volumes go last
        order.sort(key=lambda v: (v is None, v if v is not None else 0))
        return [(v, groups[v]) for v in order]

    # ------------------------------------------------------------------ #
    # download

    def download_chapter(
        self,
        composite_id: str,
        chapter: NativeChapter,
        dest: Path,
        pages_spec: str | None = None,
    ) -> Path:
        """Downloads a chapter completely, trying every available release of
        it (scanlation groups) until one provides all pages."""
        dest.mkdir(parents=True, exist_ok=True)
        marker = dest / ".complete"
        if marker.is_file():
            log.info("chapter already downloaded, skipping: %s", dest.name)
            return dest

        releases = [chapter.key, *chapter.fallbacks]
        last_err: Exception | None = None
        for n, key in enumerate(releases):
            try:
                count = self._download_release(
                    composite_id, key, chapter, dest, pages_spec
                )
            except Exception as e:  # noqa: BLE001 - try the next release
                last_err = e
                # drop partial files so the next release starts clean
                for f in dest.iterdir():
                    if f.is_file() and f.name != ".complete":
                        f.unlink()
                if n + 1 < len(releases):
                    log.warning(
                        "release %s of '%s' failed (%s) — trying another release",
                        key[:8], chapter.name, e,
                    )
                continue
            marker.write_text(str(count))
            return dest
        raise MangaDlError(f"'{chapter.name}': {last_err}") from last_err

    def _download_release(
        self,
        composite_id: str,
        release_key: str,
        chapter: NativeChapter,
        dest: Path,
        pages_spec: str | None,
    ) -> int:
        """Downloads one release (chapter entry) fully; raises on any missing
        page. Returns the number of pages written."""
        source_id, _ = composite_id.split("::", 1)
        pages = self.catalog.chapter_pages(composite_id, release_key)
        indices = parse_page_range(pages_spec, len(pages))
        log.info(
            "downloading '%s' (%d pages, release %s)",
            chapter.name, len(indices), release_key[:8],
        )

        def fetch(i: int) -> tuple[int, bytes]:
            last_err: Exception | None = None
            for attempt in range(1, self.retries + 1):
                if stop.is_set():
                    break
                try:
                    return i, self.catalog.fetch_page_image(source_id, pages[i])
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    # 4xx are permanent (missing image, forbidden hotlink):
                    # retrying with backoff only wastes time
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status is not None and 400 <= status < 500:
                        break
                    if attempt >= self.retries or stop.is_set():
                        break
                    delay = min(2**attempt, 10)
                    log.warning(
                        "page %d failed (%d/%d): %s — retrying in %ds",
                        i + 1, attempt, self.retries, e, delay,
                    )
                    # interruptible backoff: exits as soon as Ctrl+C is seen
                    if stop.wait(delay):
                        break
            raise RuntimeError(f"page {i + 1} failed: {last_err}") from last_err

        from .downloader import ChapterDownloader as _CD

        stop = threading.Event()

        failed_pages: list[str] = []
        with ThreadPoolExecutor(
            max_workers=self.workers, initializer=block_sigint
        ) as pool:
            futures = {pool.submit(fetch, i): i for i in indices}
            done = 0
            try:
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        _, data = fut.result()
                    except Exception as e:  # noqa: BLE001
                        failed_pages.append(str(i + 1))
                        log.error("page %d of '%s': %s", i + 1, chapter.name, e)
                        continue
                    ext = _CD._ext_from(data)
                    (dest / f"{i + 1:04d}{ext}").write_bytes(data)
                    done += 1
                    log.info("  [%d/%d] ok", done, len(indices))
            except KeyboardInterrupt:
                # tell the workers to stop sleeping/retrying and drop queued
                # pages, so the executor shuts down without hanging or spewing
                # thread-join noise at exit
                stop.set()
                for f in futures:
                    f.cancel()
                raise

        if failed_pages:
            # never save a chapter with missing pages: reading a manga with
            # holes is worse than failing the release outright. The caller
            # cleans up and tries the next release of the same chapter.
            raise MangaDlError(
                f"{len(failed_pages)}/{len(indices)} page(s) unavailable "
                f"({', '.join(failed_pages)})"
            )

        return len(indices)

    def download_cover(self, composite_id: str, dest_dir: Path) -> Path | None:
        """Downloads the manga cover to dest_dir (returns None if absent)."""
        from .downloader import ChapterDownloader as _CD

        source_id, _ = composite_id.split("::", 1)
        details = self.catalog.manga_details(composite_id)
        cover = details.get("cover")
        if not cover:
            return None
        data = self.catalog.fetch_page_image(source_id, cover)
        ext = _CD._ext_from(data)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"cover{ext}"
        out.write_bytes(data)
        log.info("cover saved: %s", out)
        return out
