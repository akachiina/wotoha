"""wotoha configuration via environment variables (with simple .env support)."""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Carrega um arquivo .env simples (KEY=VALUE) se existir."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_dotenv()


class Config:
    """Global configuration read from the environment."""

    def __init__(self) -> None:
        self.server_url: str = os.environ.get("MANGA_DL_URL", "http://localhost:4567").rstrip("/")
        self.username: str | None = os.environ.get("MANGA_DL_USERNAME") or None
        self.password: str | None = os.environ.get("MANGA_DL_PASSWORD") or None
        self.default_format: str = os.environ.get("MANGA_DL_FORMAT", "raw").lower()
        self.download_dir: Path = Path(os.environ.get("MANGA_DL_DIR", "downloads")).expanduser()
        self.server_jar: Path = Path(
            os.environ.get("MANGA_DL_SERVER_JAR", "~/.wotoha/Suwayomi-Server.jar")
        ).expanduser()
        self.server_log: Path = Path(
            os.environ.get("MANGA_DL_SERVER_LOG", "~/.wotoha/server.log")
        ).expanduser()
        self.server_pid: Path = Path(
            os.environ.get("MANGA_DL_SERVER_PID", "~/.wotoha/server.pid")
        ).expanduser()
        self.sources_dir: str = os.environ.get(
            "WOTOHA_SOURCES_DIR", "~/.wotoha/sources"
        )
        # rede (conceitos portados do mangadex-downloader)
        self.proxy: str | None = os.environ.get("WOTOHA_PROXY") or None
        self.timeout: float = float(os.environ.get("WOTOHA_TIMEOUT", "30"))
        # rate limit: calls per seconds; 0 disables (default). Enable via CLI
        # --delay-requests N (1 request a cada N segundos) ou env.
        self.rate_limit_calls: int = int(os.environ.get("WOTOHA_RATE_CALLS", "0"))
        self.rate_limit_seconds: float = float(os.environ.get("WOTOHA_RATE_SECONDS", "2.5"))
        # languages for sources that filter the catalog (e.g. MangaPlus)
        self.languages: str = os.environ.get("WOTOHA_LANGUAGES", "eng")


config = Config()
