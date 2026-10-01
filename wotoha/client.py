"""Client REST para o Suwayomi-Server.

API referenciada de REF/suwayomi-server/server/src/main/kotlin/suwayomi/tachidesk/manga/MangaAPI.kt
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import config
from .errors import MangaNotFound, ServerNotRunning, SourceNotFound

log = logging.getLogger(__name__)


class SuwayomiClient:
    """Client HTTP para a API do Suwayomi-Server."""

    def __init__(self, base_url: str | None = None) -> None:
        from .net import apply_network_options

        self.base_url = (base_url or config.server_url).rstrip("/")
        self.session = requests.Session()
        if config.username and config.password:
            self.session.auth = (config.username, config.password)
        self.session.headers["User-Agent"] = "wotoha/0.1"
        apply_network_options(self.session)

    # ------------------------------------------------------------------ #
    # infra
    # ------------------------------------------------------------------ #

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            resp = self.session.request(method, self._url(path), timeout=120, **kwargs)
        except requests.exceptions.ConnectionError as e:
            raise ServerNotRunning(
                f"Não foi possível conectar em {self.base_url}. "
                "Inicie o servidor com: wotoha server start"
            ) from e
        resp.raise_for_status()
        return resp

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def is_up(self) -> bool:
        """Verifica se o servidor está acessível."""
        try:
            self._request("GET", "/settings/network/timeout")
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ #
    # extensions
    # ------------------------------------------------------------------ #

    def list_extensions(self) -> list[dict[str, Any]]:
        return self._get("/extension/list").json()

    def install_extension(self, pkg_name: str) -> None:
        self._get(f"/extension/install/{pkg_name}")

    def uninstall_extension(self, pkg_name: str) -> None:
        self._get(f"/extension/uninstall/{pkg_name}")

    # ------------------------------------------------------------------ #
    # sources
    # ------------------------------------------------------------------ #

    def list_sources(self) -> list[dict[str, Any]]:
        return self._get("/source/list").json()

    def get_source(self, source_id: str) -> dict[str, Any]:
        try:
            return self._get(f"/source/{source_id}").json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise SourceNotFound(f"Source '{source_id}' não encontrado") from e
            raise

    def get_source_filters(self, source_id: str) -> list[dict[str, Any]]:
        return self._get(f"/source/{source_id}/filters").json()

    # ------------------------------------------------------------------ #
    # browsing
    # ------------------------------------------------------------------ #

    def popular(self, source_id: str, page: int = 1) -> dict[str, Any]:
        return self._get(f"/source/{source_id}/popular/{page}").json()

    def latest(self, source_id: str, page: int = 1) -> dict[str, Any]:
        return self._get(f"/source/{source_id}/latest/{page}").json()

    def search(self, source_id: str, term: str, page: int = 1) -> dict[str, Any]:
        resp = self._get(
            f"/source/{source_id}/search",
            params={"searchTerm": term, "pageNum": page},
        )
        return resp.json()

    # ------------------------------------------------------------------ #
    # manga / chapters / pages
    # ------------------------------------------------------------------ #

    def get_manga(self, manga_id: int) -> dict[str, Any]:
        try:
            return self._get(f"/manga/{manga_id}").json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise MangaNotFound(f"Mangá {manga_id} não encontrado") from e
            raise

    def get_manga_full(self, manga_id: int) -> dict[str, Any]:
        try:
            return self._get(f"/manga/{manga_id}/full").json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise MangaNotFound(f"Mangá {manga_id} não encontrado") from e
            raise

    def get_chapters(self, manga_id: int) -> list[dict[str, Any]]:
        """Lista de capítulos do mangá.

        Endpoint: GET /manga/{mangaId}/chapters
        (o /full nem sempre inclui chapters, por isso usamos o endpoint dedicado)
        """
        return self._get(f"/manga/{manga_id}/chapters").json()

    def get_chapter(self, manga_id: int, chapter_index: int) -> dict[str, Any]:
        return self._get(f"/manga/{manga_id}/chapter/{chapter_index}").json()

    def get_page_image(self, manga_id: int, chapter_index: int, page_index: int) -> bytes:
        """Baixa a imagem de uma página (o server resolve hotlink/proteção).

        Endpoint: GET /manga/{mangaId}/chapter/{chapterIndex}/page/{index}
        """
        resp = self._get(f"/manga/{manga_id}/chapter/{chapter_index}/page/{page_index}", stream=True)
        content = resp.content
        resp.close()
        return content

    def get_manga_thumbnail(self, manga_id: int) -> bytes:
        resp = self._get(f"/manga/{manga_id}/thumbnail", stream=True)
        content = resp.content
        resp.close()
        return content
