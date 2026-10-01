"""Pipeline de download de capítulos/páginas via Suwayomi-Server.

Conceitos de progresso/retries inspirados no mangadex-downloader
(REF/mangadex-downloader/mangadex_downloader/downloader.py).
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import SuwayomiClient
from .errors import ChapterNotFound, DownloadFailed, MangaDlError

log = logging.getLogger(__name__)

SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str, maxlen: int = 120) -> str:
    """Sanitiza um nome para uso em filesystem."""
    cleaned = SAFE_NAME_RE.sub("_", name).strip(" .")
    return (cleaned or "sem-nome")[:maxlen]


def parse_page_range(spec: str | None, total: int) -> list[int]:
    """Converte --pages em índices 0-based (semântica do mangadex-downloader).

    - "5"      → só a página 5
    - "1-10"   → páginas 1 a 10
    - "5-"     → da página 5 até o fim
    - "3--"    → as 3 últimas páginas
    - "-5"     → tudo menos as 5 últimas (end negativo = a partir do fim)
    """
    if not spec or not spec.strip():
        return list(range(total))
    s = re.sub(r"\s", "", spec)

    m = re.fullmatch(r"(\d+)--", s)  # últimas N
    if m:
        n = int(m.group(1))
        return list(range(max(0, total - n), total))

    m = re.fullmatch(r"-(\d+)", s)  # -N: exclui as N últimas
    if m:
        n = int(m.group(1))
        end = max(0, total - n)
        return list(range(0, end))

    m = re.fullmatch(r"(\d+)-", s)  # N-: de N até o fim
    if m:
        start = int(m.group(1))
        if not 1 <= start <= total:
            raise ChapterNotFound(f"página {start} fora do intervalo (1-{total})")
        return list(range(start - 1, total))

    m = re.fullmatch(r"(\d+)-(\d+)", s)  # faixa
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start < 1 or end < start:
            raise ChapterNotFound(f"faixa de páginas inválida: '{spec}'")
        return list(range(start - 1, min(end, total)))

    m = re.fullmatch(r"(\d+)", s)  # página única
    if m:
        page = int(m.group(1))
        if not 1 <= page <= total:
            raise ChapterNotFound(f"página {page} fora do intervalo (1-{total})")
        return [page - 1]

    raise ChapterNotFound(f"especificação de páginas inválida: '{spec}'")


@dataclass
class ChapterRef:
    """Referência a um capítulo do Suwayomi."""

    id: int
    index: int  # sourceOrder do Suwayomi (1-based)
    name: str
    chapter_number: float
    scanlator: str | None
    page_count: int
    volume_number: float | None = None  # Suwayomi não expõe volume

    @property
    def is_oneshot(self) -> bool:
        import re as _re

        return self.chapter_number in (None, 0) and bool(
            _re.search(r"one[\s-]?shot", self.name or "", _re.IGNORECASE)
        )


class ChapterDownloader:
    """Baixa capítulos de um mangá para o disco."""

    def __init__(self, client: SuwayomiClient, workers: int = 4, retries: int = 3) -> None:
        self.client = client
        self.workers = workers
        self.retries = retries

    # ------------------------------------------------------------------ #

    def list_chapters(self, manga_id: int, no_oneshot: bool = False) -> list[ChapterRef]:
        """Lista de capítulos (ordenados do mais recente para o mais antigo,
        como o Suwayomi retorna)."""
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
        """Seleciona capítulos conforme especificação do usuário.

        - None ou "all": todos
        - "latest": apenas o capítulo mais recente
        - "1-10,15,20-23": intervalos por **número do capítulo** (não índice)
        - start_chapter/end_chapter: recorte numérico adicional
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
                    raise MangaDlError(f"Faixa inválida: '{part}'") from e
                for num in by_number:
                    if start <= num <= end:
                        selected[num] = by_number[num]
            else:
                try:
                    num = float(part)
                except ValueError as e:
                    raise MangaDlError(f"Número de capítulo inválido: '{part}'") from e
                if num in by_number:
                    selected[num] = by_number[num]
                else:
                    log.warning("Capítulo %s não encontrado", part)
        # mantém a ordem original (mais recente primeiro)
        return [c for c in chapters if c.id in {s.id for s in selected.values()}]

    # ------------------------------------------------------------------ #

    def _fetch_page(self, manga_id: int, chapter_index: int, page_index: int) -> bytes:
        """Baixa uma página com retries e backoff exponencial."""
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                data = self.client.get_page_image(manga_id, chapter_index, page_index)
                if not data:
                    raise DownloadFailed("resposta vazia")
                return data
            except Exception as e:  # noqa: BLE001 - retry genérico
                last_err = e
                delay = min(2 ** attempt, 10)
                log.warning(
                    "Página %d falhou (tentativa %d/%d): %s — retry em %ds",
                    page_index, attempt, self.retries, e, delay,
                )
                time.sleep(delay)
        raise DownloadFailed(
            f"Página {page_index} falhou após {self.retries} tentativas"
        ) from last_err

    def download_chapter(
        self,
        manga: dict[str, Any],
        chapter: ChapterRef,
        dest: Path,
        pages_spec: str | None = None,
    ) -> Path:
        """Baixa as páginas de um capítulo para `dest`.

        Fluxo do Suwayomi: a primeira chamada a /page/0 faz o servidor buscar a
        lista de páginas no site (populate do PageTable) e atualizar o pageCount
        do capítulo. Por isso "aquecemos" com a página 0 antes de paralelizar.
        """
        dest.mkdir(parents=True, exist_ok=True)
        marker = dest / ".complete"
        if marker.is_file():
            log.info("Capítulo já baixado, pulando: %s", dest.name)
            return dest

        log.info("Baixando '%s'", chapter.name)

        # 1) GET /chapter/{index}: faz o server popular pageCount e a lista de páginas
        info = self.client.get_chapter(manga["id"], chapter.index)
        page_count = int(info.get("pageCount") or 0)
        if page_count < 1:
            raise DownloadFailed(
                f"pageCount inválido ({page_count}) para o capítulo '{chapter.name}'"
            )

        # 2) índices a baixar (respeita --pages)
        indices = parse_page_range(pages_spec, page_count)
        log.info("  %d página(s)%s", len(indices), f" de {page_count}" if len(indices) < page_count else "")

        # aquecimento: popula a lista de páginas no server (sempre página 0)
        if 0 not in indices:
            self._fetch_page(manga["id"], chapter.index, 0)

        def fetch(i: int) -> tuple[int, bytes]:
            return i, self._fetch_page(manga["id"], chapter.index, i)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fetch, i): i for i in indices}
            done = 0
            for fut in as_completed(futures):
                page_index, data = fut.result()
                ext = self._ext_from(data)
                (dest / f"{page_index + 1:04d}{ext}").write_bytes(data)
                done += 1
                log.info("  [%d/%d] ok", done, len(indices))

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
