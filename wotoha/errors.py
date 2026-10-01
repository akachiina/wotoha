"""manga-dl exceptions."""

from __future__ import annotations


class MangaDlError(Exception):
    """Erro base do manga-dl."""


class ServerNotRunning(MangaDlError):
    """The Suwayomi-Server is not reachable at the configured URL."""


class ServerJarNotFound(MangaDlError):
    """The Suwayomi-Server jar was not found."""


class SourceNotFound(MangaDlError):
    """Source not found on the server."""


class MangaNotFound(MangaDlError):
    """Manga not found."""


class ChapterNotFound(MangaDlError):
    """Chapter not found."""


class JavaNotFound(MangaDlError):
    """Java not found on the system (required by Suwayomi-Server)."""


class DownloadFailed(MangaDlError):
    """Failed to download a page/chapter."""
