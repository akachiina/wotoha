"""Exceções do manga-dl."""

from __future__ import annotations


class MangaDlError(Exception):
    """Erro base do manga-dl."""


class ServerNotRunning(MangaDlError):
    """O Suwayomi-Server não está acessível na URL configurada."""


class ServerJarNotFound(MangaDlError):
    """O jar do Suwayomi-Server não foi encontrado."""


class SourceNotFound(MangaDlError):
    """Source não encontrado no servidor."""


class MangaNotFound(MangaDlError):
    """Mangá não encontrado."""


class ChapterNotFound(MangaDlError):
    """Capítulo não encontrado."""


class JavaNotFound(MangaDlError):
    """Java não encontrado no sistema (necessário para o Suwayomi-Server)."""


class DownloadFailed(MangaDlError):
    """Falha ao baixar uma página/capítulo."""
