"""wotoha serve — local web UI.

A tiny threaded HTTP server (stdlib only) that exposes the same features as
the CLI: search, manga details, an online reader and a download queue —
styled after the illustrator Wotoha (OFUTONE): cozy "futon" pastels,
soft clouds and her little jellyfish mascot.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .aidoku_catalog import AidokuCatalog
from .config import config
from .errors import MangaDlError
from .formats import get_format
from .native_downloader import NativeChapter, NativeDownloader

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web_static"


# --------------------------------------------------------------------------- #
# download queue
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    id: int
    manga_id: str
    title: str
    chapter_keys: list[str]
    fmt: str
    mode: str
    total: int
    done: int = 0
    current: str = ""
    state: str = "queued"  # queued | running | done | error | cancelled
    error: str = ""
    started: float = field(default_factory=time.time)


class DownloadQueue:
    """Background worker that processes download jobs one at a time."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._cancel: set[int] = set()
        self._worker: threading.Thread | None = None

    def enqueue(
        self,
        catalog: AidokuCatalog,
        manga_id: str,
        title: str,
        chapter_keys: list[str],
        fmt: str,
        mode: str,
    ) -> Job:
        job = Job(
            id=self._next_id,
            manga_id=manga_id,
            title=title,
            chapter_keys=list(chapter_keys),
            fmt=fmt,
            mode=mode,
            total=len(chapter_keys),
        )
        self._next_id += 1
        with self._lock:
            self.jobs.append(job)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, args=(catalog,), daemon=True
            )
            self._worker.start()
        return job

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            for j in self.jobs:
                if j.id == job_id and j.state in ("queued", "running"):
                    j.state = "cancelled"
                    self._cancel.add(job_id)
                    return True
        return False

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": j.id,
                    "mangaId": j.manga_id,
                    "title": j.title,
                    "total": j.total,
                    "done": j.done,
                    "current": j.current,
                    "state": j.state,
                    "error": j.error,
                    "format": j.fmt,
                    "mode": j.mode,
                }
                for j in self.jobs
            ]

    # -- worker ---------------------------------------------------------- #

    def _run(self, catalog: AidokuCatalog) -> None:
        while True:
            job = None
            with self._lock:
                for j in self.jobs:
                    if j.state == "queued":
                        job = j
                        break
            if job is None:
                return
            try:
                self._process(catalog, job)
            except Exception as e:  # noqa: BLE001
                if job.id in self._cancel:
                    job.state = "cancelled"
                else:
                    job.state = "error"
                    job.error = str(e)[:300]
                    log.exception("job %s failed", job.id)

    def _process(self, catalog: AidokuCatalog, job: Job) -> None:
        job.state = "running"
        dl = NativeDownloader(catalog, workers=4, retries=3)
        details = catalog.manga_details(job.manga_id)
        manga_stub = {
            "title": details["title"],
            "id": job.manga_id,
            "url": details.get("url"),
            "authors": details.get("authors"),
            "artists": details.get("artists"),
            "description": details.get("description"),
            "tags": details.get("tags"),
            "lang": job.manga_id.split("::")[0].split(".")[0] or None,
        }
        fmt = get_format(job.fmt, config.download_dir, mode=job.mode)
        base_tmp = config.download_dir / ".cache"
        base_tmp.mkdir(parents=True, exist_ok=True)

        from .downloader import safe_name

        all_chapters = dl.list_chapters(job.manga_id)
        by_key = {c.key: c for c in all_chapters}
        for key in job.chapter_keys:
            if job.id in self._cancel:
                return
            chapter: NativeChapter | None = by_key.get(key)
            if chapter is None:
                job.done += 1
                continue
            job.current = chapter.name
            workdir = base_tmp / safe_name(f"{job.manga_id}-{key}")
            workdir.mkdir(parents=True, exist_ok=True)
            dl.download_chapter(job.manga_id, chapter, workdir)
            if job.mode == "volume":
                fmt.save_volume(manga_stub, chapter, workdir)
            else:
                fmt.save_chapter(manga_stub, chapter, workdir)
            job.done += 1
            job.current = ""
        fmt.finish(manga_stub)
        if job.id not in self._cancel:
            job.state = "done"
            job.current = ""


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


def make_handler(catalog: AidokuCatalog, queue: DownloadQueue):
    """Builds the request handler bound to the shared catalog + queue."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "wotoha"

        # -- plumbing ---------------------------------------------------- #

        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("web: %s", fmt % args)

        def _json(self, data, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name: str, ctype: str) -> None:
            path = STATIC_DIR / name
            if not path.is_file():
                self._json({"error": "not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _image(self, data: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", _sniff_image_mime(data))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def _qs(self) -> dict[str, str]:
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        # -- routes ------------------------------------------------------ #

        def do_GET(self):  # noqa: N802
            try:
                self._route_get()
            except BrokenPipeError:
                pass
            except MangaDlError as e:
                self._json({"error": str(e)}, 502)
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s failed", self.path)
                self._json({"error": str(e)}, 500)

        def do_POST(self):  # noqa: N802
            try:
                self._route_post()
            except Exception as e:  # noqa: BLE001
                log.exception("POST %s failed", self.path)
                self._json({"error": str(e)}, 500)

        def _route_get(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/static/style.css":
                return self._static("style.css", "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            if path == "/static/favicon.svg":
                return self._static("favicon.svg", "image/svg+xml")
            if path == "/favicon.ico":
                return self._static("favicon.svg", "image/svg+xml")

            qs = self._qs()
            if path == "/api/sources":
                from .aidoku_store import list_installed, repo_of

                return self._json(
                    [
                        {"id": s["id"], "name": s["name"], "repo": repo_of(s["id"])}
                        for s in list_installed()
                    ]
                )
            if path == "/api/search":
                source = qs.get("source", "")
                q = qs.get("q", "")
                page = int(qs.get("page", "1"))
                lang = qs.get("lang") or None
                if lang:
                    config.languages = lang
                if source == "*":
                    results = []
                    for s in [x["id"] for x in _list_sources()]:
                        try:
                            results.extend(catalog.search(s, q, page)["mangaList"])
                        except Exception:  # noqa: BLE001
                            continue
                    return self._json({"mangaList": results, "hasNextPage": False})
                data = catalog.search(source, q, page)
                return self._json(data)
            if path == "/api/manga":
                lang = qs.get("lang") or None
                if lang:
                    config.languages = lang
                return self._json(catalog.manga_details(qs.get("id", "")))
            if path == "/api/cover":
                return self._image(
                    _fetch_cached(
                        catalog, qs.get("url", ""), "covers", qs.get("source") or None
                    )
                )
            if path == "/api/pages":
                manga_id = qs.get("id", "")
                chapter_key = qs.get("chapter", "")
                pages = catalog.chapter_pages(manga_id, chapter_key)
                return self._json({"count": len(pages)})
            if path == "/api/page":
                manga_id = qs.get("id", "")
                chapter_key = qs.get("chapter", "")
                n = int(qs.get("n", "0"))
                source_id, key = manga_id.split("::", 1)
                pages = catalog.chapter_pages(manga_id, chapter_key)
                if 0 <= n < len(pages):
                    return self._image(
                        _fetch_cached(
                            catalog, pages[n], f"pages/{source_id}/{key}", source_id
                        )
                    )
                return self._json({"error": "page out of range"}, 404)
            if path == "/api/jobs":
                return self._json(queue.snapshot())
            return self._json({"error": "not found"}, 404)

        def _route_post(self) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/download":
                manga_id = payload.get("mangaId", "")
                details = catalog.manga_details(manga_id)
                lang = payload.get("lang") or None
                if lang:
                    config.languages = lang
                job = queue.enqueue(
                    catalog,
                    manga_id,
                    details.get("title") or manga_id,
                    payload.get("chapters", []),
                    payload.get("format", "cbz"),
                    payload.get("mode", "chapter"),
                )
                return self._json({"jobId": job.id})
            if url.path == "/api/jobs/cancel":
                ok = queue.cancel(int(payload.get("id", 0)))
                return self._json({"ok": ok})
            return self._json({"error": "not found"}, 404)

    return Handler


def _list_sources() -> list[dict]:
    from .aidoku_store import list_installed

    return list_installed()


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] in (b"II", b"MM"):
        return "image/tiff"
    return "application/octet-stream"


# cover/page cache shared across requests (~/.wotoha/cache/...)
_CACHE_LOCK = threading.Lock()


def _fetch_cached(
    catalog: AidokuCatalog, url: str, subdir: str, source_id: str | None = None
) -> bytes:
    """Fetches a remote image through the source runtime, cached on disk."""
    if not url:
        raise MangaDlError("missing image url")
    from .downloader import safe_name

    cache_dir = Path(config.sources_dir).expanduser().parent / "cache" / subdir
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / safe_name(url.replace("https://", "").replace("http://", ""))[:200]
    if dest.is_file() and dest.stat().st_size > 0:
        return dest.read_bytes()
    if source_id is None:
        # find which installed source can fetch this url (its base domain)
        for s in _list_sources():
            base = catalog.runtime_for(s["id"]).base_url
            if base and base.replace("https://", "").replace("http://", "").split("/")[0] in url:
                source_id = s["id"]
                break
    if source_id is None:
        source_id = _list_sources()[0]["id"]  # fallback: first runtime
    data = catalog.fetch_image(source_id, url)
    with _CACHE_LOCK:
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return data


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def serve(host: str = "127.0.0.1", port: int = 8321, open_browser: bool = False) -> None:
    catalog = AidokuCatalog()
    queue = DownloadQueue()
    httpd = ThreadingHTTPServer((host, port), make_handler(catalog, queue))
    url = f"http://{host}:{port}"
    print(f"\n  🪼 wotoha web — {url}\n")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye! 🪼")
