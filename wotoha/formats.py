"""wotoha output formats: raw, epub, cbz, cb7 and pdf.

Design ported from mangadex-downloader
(REF/mangadex-downloader/mangadex_downloader/format/):
- ComicInfoPlugin (ComicRack metadata inside the file)
- VolumeEpub/Cbz (per-volume packaging)
- SingleEpub/Cbz (all chapters in a single file)
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .comicinfo import comicinfo_xml
from .downloader import safe_name
from .errors import MangaDlError

log = logging.getLogger(__name__)

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def _mime(img: Path) -> str:
    return MIME_BY_EXT.get(img.suffix.lower(), "application/octet-stream")


def image_files(images_dir: Path) -> list[Path]:
    """List images in the directory, ignoring metadata (e.g. .complete)."""
    return sorted(p for p in images_dir.iterdir() if p.suffix.lower() in MIME_BY_EXT)


# --------------------------------------------------------------------------- #
# chapter/number helpers
# --------------------------------------------------------------------------- #


def _chapter_title(chapter) -> str:
    return (
        getattr(chapter, "title", None)
        or getattr(chapter, "name", None)
        or "Chapter"
    )


def _num(value) -> str | None:
    return f"{value:g}" if value is not None else None


def _chapter_number(chapter):
    return getattr(chapter, "chapter_number", None)


def _volume_number(chapter):
    return getattr(chapter, "volume_number", None)


def _date_uploaded(chapter):
    return getattr(chapter, "date_uploaded", None)


def _authors(manga) -> list[str]:
    authors = manga.get("authors") or []
    if not authors and manga.get("author"):
        authors = [manga["author"]]
    return authors


def _artists(manga) -> list[str]:
    return manga.get("artists") or _authors(manga)


def _web(manga) -> str:
    return manga.get("url") or str(manga.get("id") or "")


def _page_number(img: Path) -> int:
    try:
        return int(img.stem)
    except ValueError:
        return 0


def _volume_folder_name(chapter) -> str:
    vol = _volume_number(chapter)
    if vol is not None:
        return f"Volume {vol:g}"
    # sources without volume data: group everything into a single "No Volume" group
    # (same behavior as mangadex-downloader's VolumePlugin)
    return "No Volume"


class _Staging:
    """Temporary area to accumulate images across chapters (volume/single).

    Needed because the CLI deletes each chapter's workdir after packing it.
    """

    def __init__(self, base_dir: Path, key: str) -> None:
        self.dir = base_dir / ".wotoha-staging" / safe_name(key)
        self._count = 0

    def add(self, images: list[Path]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for img in images:
            self._count += 1
            dst = self.dir / f"{self._count:04d}{img.suffix}"
            shutil.copy2(img, dst)

    def images(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return image_files(self.dir)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _cleanup_staging(base_dir: Path) -> None:
    shutil.rmtree(base_dir / ".wotoha-staging", ignore_errors=True)


# --------------------------------------------------------------------------- #
# base strategy
# --------------------------------------------------------------------------- #


class OutputFormat(ABC):
    """Output strategy: how to pack the downloaded chapters."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.one_file = False  # single mode (everything in one file/folder)

    @abstractmethod
    def _save_chapter(self, manga: dict[str, Any], chapter: Any, images_dir: Path) -> None:
        """Takes the dir with the chapter images and produces the final output."""

    def _save_volume(self, manga, chapter, images_dir: Path) -> None:
        raise MangaDlError(f"format {type(self).__name__} does not support volume mode")

    def save_chapter(self, manga, chapter, images_dir: Path) -> None:
        self._save_chapter(manga, chapter, images_dir)

    def save_volume(self, manga, chapter, images_dir: Path) -> None:
        self._save_volume(manga, chapter, images_dir)

    def finish(self, manga: dict[str, Any]) -> None:
        """Called after all chapters are processed."""
        _cleanup_staging(self.base_dir)


# --------------------------------------------------------------------------- #
# ComicInfo builders
# --------------------------------------------------------------------------- #


def _chapter_comicinfo(manga, chapter) -> str:
    return comicinfo_xml(
        title=_chapter_title(chapter),
        series=manga["title"],
        number=_num(_chapter_number(chapter)),
        volume=_num(_volume_number(chapter)),
        summary=manga.get("description"),
        writer=", ".join(_authors(manga)) or None,
        penciller=", ".join(_artists(manga)) or None,
        tags=manga.get("tags"),
        web=_web(manga),
        **_date_parts(_date_uploaded(chapter)),
    )


def _volume_comicinfo(manga, chapter) -> str:
    vol = _volume_number(chapter)
    return comicinfo_xml(
        title=f"{manga['title']} {_volume_folder_name(chapter)}",
        series=manga["title"],
        volume=_num(vol),
        summary=manga.get("description"),
        writer=", ".join(_authors(manga)) or None,
        penciller=", ".join(_artists(manga)) or None,
        tags=manga.get("tags"),
        web=_web(manga),
    )


def _manga_comicinfo(manga) -> str:
    return comicinfo_xml(
        title=manga["title"],
        series=manga["title"],
        summary=manga.get("description"),
        writer=", ".join(_authors(manga)) or None,
        penciller=", ".join(_artists(manga)) or None,
        tags=manga.get("tags"),
        web=_web(manga),
    )


def _date_parts(ts) -> dict:
    if not ts:
        return {}
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return {"year": dt.year, "month": dt.month, "day": dt.day}
    except (ValueError, OSError, OverflowError):
        return {}


# --------------------------------------------------------------------------- #
# raw
# --------------------------------------------------------------------------- #


class RawFormat(OutputFormat):
    """Loose images in folders, mangadex-downloader style."""

    def _manga_dir(self, manga) -> Path:
        d = self.base_dir / safe_name(manga["title"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_chapter(self, manga, chapter, images_dir: Path) -> None:
        final = self._manga_dir(manga) / safe_name(chapter.name)
        if final.exists():
            log.info("Folder already exists, skipping: %s", final.name)
            return
        images_dir.rename(final)
        log.info("Saved to: %s", final)

    def _save_chapter_single(self, manga, chapter, images_dir: Path) -> None:
        # stores per chapter; the single-folder merge happens in finish()
        self._save_chapter(manga, chapter, images_dir)

    def _save_volume(self, manga, chapter, images_dir: Path) -> None:
        final = self._manga_dir(manga) / safe_name(_volume_folder_name(chapter))
        final.mkdir(parents=True, exist_ok=True)
        offset = len(image_files(final))
        for img in image_files(images_dir):
            img.rename(final / f"{offset + _page_number(img):04d}{img.suffix}")
        log.info("Volume acumulado: %s", final.name)

    def finish(self, manga: dict[str, Any]) -> None:
        if self.one_file:
            # single: merge everything into one folder with sequential numbering
            import shutil as _shutil

            manga_dir = self._manga_dir(manga)
            singles = sorted(
                p for p in manga_dir.iterdir()
                if p.is_dir() and p.name != safe_name(manga["title"])
            )
            if singles:
                all_dir = manga_dir / safe_name(manga["title"])
                all_dir.mkdir(exist_ok=True)
                offset = len(image_files(all_dir))
                for sub in singles:
                    for img in image_files(sub):
                        offset += 1
                        img.rename(all_dir / f"{offset:04d}{img.suffix}")
                    _shutil.rmtree(sub)  # also removes .complete markers
                log.info("Single folder: %s", all_dir)
        super().finish(manga)


# --------------------------------------------------------------------------- #
# zip-based (cbz / cb7)
# --------------------------------------------------------------------------- #


def _zip_image_count(out: Path) -> int:
    """Number of images already in the archive (to continue numbering)."""
    try:
        if out.suffix.lower() == ".cb7":
            import py7zr

            with py7zr.SevenZipFile(out) as sz:
                return sum(
                    1 for n in sz.list() if Path(n.filename).suffix.lower() in MIME_BY_EXT
                )
        with zipfile.ZipFile(out) as zf:
            return sum(
                1 for n in zf.namelist() if Path(n).suffix.lower() in MIME_BY_EXT
            )
    except Exception:  # noqa: BLE001
        return 0


def _open_archive(out: Path, *, append: bool = False):
    if out.suffix.lower() == ".cb7":
        try:
            import py7zr
        except ImportError as e:
            raise MangaDlError(
                "py7zr is required for cb7: pip install wotoha[cb7]"
            ) from e
        return py7zr.SevenZipFile(out, "a" if append else "w")
    return zipfile.ZipFile(out, "a" if append else "w", compression=zipfile.ZIP_STORED)


def _archive_writestr(arc, arcname: str, text: str) -> None:
    if type(arc).__module__.startswith("py7zr"):
        # py7zr has the argument order swapped compared to zipfile
        arc.writestr(text, arcname)
    else:
        arc.writestr(arcname, text)


class _ZipOutput(OutputFormat):
    ext = ".zip"

    def _manga_dir(self, manga) -> Path:
        d = self.base_dir / safe_name(manga["title"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- chapter mode -------------------------------------------------- #

    def _save_chapter(self, manga, chapter, images_dir: Path) -> None:
        out = self._manga_dir(manga) / f"{safe_name(chapter.name)}{self.ext}"
        if out.is_file():
            log.info("%s already exists, skipping: %s", self.ext.upper(), out.name)
            return
        with _open_archive(out) as arc:
            for img in image_files(images_dir):
                arc.write(img, arcname=img.name)
            _archive_writestr(arc, "ComicInfo.xml", _chapter_comicinfo(manga, chapter))
        log.info("%s criado: %s", self.ext.upper(), out.name)

    # -- volume mode --------------------------------------------------- #

    def _save_volume(self, manga, chapter, images_dir: Path) -> None:
        out = self._manga_dir(manga) / f"{safe_name(_volume_folder_name(chapter))}{self.ext}"
        append = out.is_file()
        offset = _zip_image_count(out) if append else 0
        with _open_archive(out, append=append) as arc:
            for img in image_files(images_dir):
                arc.write(img, arcname=f"{offset + _page_number(img):04d}{img.suffix}")
            if not append:
                _archive_writestr(arc, "ComicInfo.xml", _volume_comicinfo(manga, chapter))
        log.info("%s updated: %s (+%d pages)", self.ext.upper(), out.name, len(image_files(images_dir)))

    # -- single mode --------------------------------------------------- #

    def _save_chapter_single(self, manga, chapter, images_dir: Path) -> None:
        out = self._manga_dir(manga) / f"{safe_name(manga['title'])}{self.ext}"
        append = out.is_file()
        offset = _zip_image_count(out) if append else 0
        with _open_archive(out, append=append) as arc:
            for img in image_files(images_dir):
                arc.write(img, arcname=f"{offset + _page_number(img):04d}{img.suffix}")
        log.info("%s: +%d pages", self.ext.upper(), len(image_files(images_dir)))

    def finish(self, manga: dict[str, Any]) -> None:
        if self.one_file:
            out = self._manga_dir(manga) / f"{safe_name(manga['title'])}{self.ext}"
            if out.is_file():
                with _open_archive(out, append=True) as arc:
                    _archive_writestr(arc, "ComicInfo.xml", _manga_comicinfo(manga))
                log.info("%s finalizado: %s", self.ext.upper(), out.name)
        super().finish(manga)


class CbzFormat(_ZipOutput):
    ext = ".cbz"


class Cb7Format(_ZipOutput):
    ext = ".cb7"


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #

EPUB_CONTAINER = (
    '<?xml version="1.0"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles>\n'
    '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
    '  </rootfiles>\n'
    '</container>\n'
)


def _page_xhtml(page_num: int, img_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title>Page %d</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
        '</head>\n'
        '<body>\n'
        '  <div class="page"><img src="../images/%s" alt="Page %d"/></div>\n'
        '</body>\n'
        '</html>\n'
    ) % (page_num, img_name, page_num)


class EpubFormat(OutputFormat):
    """One .epub per chapter/volume, compatible with e-readers.

    Simplificado do EpubPlugin do mangadex-downloader
    (REF/mangadex-downloader/mangadex_downloader/format/epub.py).
    """

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir)
        self._vol_stagings: dict[str, _Staging] = {}
        self._single_staging: _Staging | None = None

    def _manga_dir(self, manga) -> Path:
        d = self.base_dir / safe_name(manga["title"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_epub(
        self,
        out: Path,
        manga,
        title: str,
        number: str | None,
        volume: str | None,
        images: list[Path],
        comicinfo: str | None = None,
    ) -> None:
        uid = f"urn:uuid:wotoha-{abs(hash((manga['title'], title)))}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lang = manga.get("lang") or "pt"

        with zipfile.ZipFile(out, "w") as zf:
            # mimetype must be the first entry, uncompressed
            zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
            zf.writestr("META-INF/container.xml", EPUB_CONTAINER)
            for i, img in enumerate(images, start=1):
                zf.write(img, f"OEBPS/images/{img.name}")
                zf.writestr(f"OEBPS/text/p{i}.xhtml", _page_xhtml(i, img.name))
            zf.writestr(
                "OEBPS/style.css",
                "body{margin:0}.page{text-align:center}img{max-width:100%;max-height:100%}",
            )
            zf.writestr(
                "OEBPS/content.opf", self._opf(manga, uid, lang, now, title, images)
            )
            zf.writestr("OEBPS/toc.ncx", self._ncx(uid, title, len(images)))
            if comicinfo:
                zf.writestr("OEBPS/ComicInfo.xml", comicinfo)

    # -- chapter mode -------------------------------------------------- #

    def _save_chapter(self, manga, chapter, images_dir: Path) -> None:
        out = self._manga_dir(manga) / f"{safe_name(chapter.name)}.epub"
        if out.is_file():
            log.info("EPUB already exists, skipping: %s", out.name)
            return
        self._write_epub(
            out,
            manga,
            title=_chapter_title(chapter),
            number=_num(_chapter_number(chapter)),
            volume=_num(_volume_number(chapter)),
            images=image_files(images_dir),
            comicinfo=_chapter_comicinfo(manga, chapter),
        )
        log.info("EPUB criado: %s", out.name)

    # -- volume mode --------------------------------------------------- #

    def _save_volume(self, manga, chapter, images_dir: Path) -> None:
        vol_name = _volume_folder_name(chapter)
        staging = self._vol_stagings.setdefault(vol_name, _Staging(self.base_dir, f"vol-{vol_name}"))
        staging.add(image_files(images_dir))
        out = self._manga_dir(manga) / f"{safe_name(vol_name)}.epub"
        self._write_epub(
            out,
            manga,
            title=f"{manga['title']} {vol_name}",
            number=None,
            volume=_num(_volume_number(chapter)),
            images=staging.images(),
            comicinfo=_volume_comicinfo(manga, chapter),
        )
        log.info("EPUB do volume atualizado: %s", out.name)

    # -- single mode --------------------------------------------------- #

    def _save_chapter_single(self, manga, chapter, images_dir: Path) -> None:
        if self._single_staging is None:
            self._single_staging = _Staging(self.base_dir, "single")
        self._single_staging.add(image_files(images_dir))

    def finish(self, manga: dict[str, Any]) -> None:
        if self.one_file and self._single_staging is not None:
            out = self._manga_dir(manga) / f"{safe_name(manga['title'])}.epub"
            images = self._single_staging.images()
            if images:
                self._write_epub(
                    out,
                    manga,
                    title=manga["title"],
                    number=None,
                    volume=None,
                    images=images,
                    comicinfo=_manga_comicinfo(manga),
                )
                log.info("Single EPUB created: %s (%d pages)", out.name, len(images))
        super().finish(manga)

    # -- OPF/NCX -------------------------------------------------------- #

    def _opf(self, manga, uid, lang, now, title, images):
        esc_title = escape(title)
        author = escape(", ".join(_authors(manga)) or "Unknown")
        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">',
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
            f'    <dc:identifier id="bookid">{escape(uid)}</dc:identifier>',
            f'    <dc:title>{esc_title}</dc:title>',
            f'    <dc:language>{escape(lang)}</dc:language>',
            f'    <dc:creator>{author}</dc:creator>',
            f'    <meta property="dcterms:modified">{now}</meta>',
            '  </metadata>',
            '  <manifest>',
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="css" href="style.css" media-type="text/css"/>',
        ]
        for i, img in enumerate(images, start=1):
            lines.append(
                f'    <item id="img{i}" href="images/{img.name}" media-type="{_mime(img)}"/>'
            )
            lines.append(
                f'    <item id="p{i}" href="text/p{i}.xhtml" media-type="application/xhtml+xml"/>'
            )
        lines.append('  </manifest>')
        lines.append('  <spine toc="ncx">')
        for i in range(1, len(images) + 1):
            lines.append(f'    <itemref idref="p{i}"/>')
        lines.append('  </spine>')
        lines.append('</package>')
        return "\n".join(lines)

    def _ncx(self, uid, title, page_count):
        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
            '  <head>',
            f'    <meta name="dtb:uid" content="{escape(uid)}"/>',
            '  </head>',
            '  <docTitle><text>' + escape(title) + '</text></docTitle>',
            '  <navMap>',
        ]
        for i in range(1, page_count + 1):
            lines.append(
                f'    <navPoint id="np{i}" playOrder="{i}">'
                f'<navLabel><text>Page {i}</text></navLabel>'
                f'<content src="text/p{i}.xhtml"/>'
                '</navPoint>'
            )
        lines.append('  </navMap>')
        lines.append('</ncx>')
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


class PdfFormat(OutputFormat):
    """One .pdf per chapter/volume (Pillow)."""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir)
        self._vol_stagings: dict[str, _Staging] = {}
        self._single_staging: _Staging | None = None

    def _manga_dir(self, manga) -> Path:
        d = self.base_dir / safe_name(manga["title"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _images_to_pdf(images: list[Path], out: Path) -> None:
        from PIL import Image as PILImage

        pages = []
        try:
            for img in images:
                with PILImage.open(img) as im:
                    pages.append(im.convert("RGB"))
            if pages:
                pages[0].save(out, save_all=True, append_images=pages[1:])
        finally:
            for p in pages:
                p.close()

    def _save_chapter(self, manga, chapter, images_dir: Path) -> None:
        out = self._manga_dir(manga) / f"{safe_name(chapter.name)}.pdf"
        if out.is_file():
            log.info("PDF already exists, skipping: %s", out.name)
            return
        images = image_files(images_dir)
        if not images:
            raise MangaDlError("nenhuma imagem para converter em PDF")
        self._images_to_pdf(images, out)
        log.info("PDF criado: %s", out.name)

    def _save_volume(self, manga, chapter, images_dir: Path) -> None:
        vol_name = _volume_folder_name(chapter)
        staging = self._vol_stagings.setdefault(vol_name, _Staging(self.base_dir, f"vol-{vol_name}"))
        staging.add(image_files(images_dir))
        out = self._manga_dir(manga) / f"{safe_name(vol_name)}.pdf"
        self._images_to_pdf(staging.images(), out)
        log.info("PDF do volume atualizado: %s", out.name)

    def _save_chapter_single(self, manga, chapter, images_dir: Path) -> None:
        if self._single_staging is None:
            self._single_staging = _Staging(self.base_dir, "single")
        self._single_staging.add(image_files(images_dir))

    def finish(self, manga: dict[str, Any]) -> None:
        if self.one_file and self._single_staging is not None:
            out = self._manga_dir(manga) / f"{safe_name(manga['title'])}.pdf"
            images = self._single_staging.images()
            if images:
                self._images_to_pdf(images, out)
                log.info("Single PDF created: %s (%d pages)", out.name, len(images))
        super().finish(manga)


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #

FORMATS: dict[str, type[OutputFormat]] = {
    "raw": RawFormat,
    "cbz": CbzFormat,
    "cb7": Cb7Format,
    "epub": EpubFormat,
    "pdf": PdfFormat,
}


def get_format(name: str, base_dir: Path, mode: str = "chapter") -> OutputFormat:
    """Creates the output strategy.

    mode: "chapter" | "volume" | "single" (single = one file for the whole manga).
    """
    try:
        cls = FORMATS[name.lower()]
    except KeyError:
        raise MangaDlError(
            f"Unknown format: '{name}'. Options: {', '.join(FORMATS)}"
        ) from None

    fmt = cls(base_dir)
    if mode == "single":
        # routes save_chapter -> single accumulation
        fmt.save_chapter = fmt._save_chapter_single  # type: ignore[method-assign]
        fmt.one_file = True
    return fmt
