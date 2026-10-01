"""ComicInfo.xml — metadata in the ComicRack/Anansi format (Kavita, Komga, Tachiyomi).

Portado do ComicInfoPlugin do mangadex-downloader
(REF/mangadex-downloader/mangadex_downloader/format/comicinfo.py).
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

AGE_RATING = {
    "Unknown": "Unknown",
    "Ongoing": "Unknown",
    "Completed": "Unknown",
    "Cancelled": "Unknown",
    "Hiatus": "Unknown",
}

MANGA_STATUS = {
    "Unknown": "Unknown",
    "Ongoing": "Ongoing",
    "Completed": "Completed",
    "Cancelled": "Abandoned Series",
    "Hiatus": "Hiatus",
}


def comicinfo_xml(
    title: str,
    series: str,
    *,
    number: str | None = None,
    volume: str | None = None,
    summary: str | None = None,
    writer: str | None = None,
    penciller: str | None = None,
    tags: list[str] | None = None,
    web: str | None = None,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> str:
    """Gera o XML do ComicInfo.xml."""
    def _v(value) -> str:
        return escape(str(value)) if value not in (None, "") else ""

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">',
        "  <ComicInfo>",
        f"    <Title>{_v(title)}</Title>",
        f"    <Series>{_v(series)}</Series>",
    ]
    if number is not None:
        lines.append(f"    <Number>{_v(number)}</Number>")
    if volume is not None:
        lines.append(f"    <Volume>{_v(volume)}</Volume>")
    if summary:
        lines.append(f"    <Summary>{_v(summary)}</Summary>")
    if writer:
        lines.append(f"    <Writer>{_v(writer)}</Writer>")
    if penciller:
        lines.append(f"    <Penciller>{_v(penciller)}</Penciller>")
    if tags:
        lines.append(f"    <Genre>{_v(', '.join(tags))}</Genre>")
    if web:
        lines.append(f"    <Web>{_v(web)}</Web>")
    if year:
        lines.append(f"    <Year>{_v(year)}</Year>")
    if month:
        lines.append(f"    <Month>{_v(month)}</Month>")
    if day:
        lines.append(f"    <Day>{_v(day)}</Day>")
    lines.append("    <LanguageISO>en</LanguageISO>")
    lines.append("    <Format>Limited Series</Format>")
    lines.append("    <Manga>Yes</Manga>")
    lines.append("  </ComicInfo>")
    lines.append("</xsd:schema>")
    return "\n".join(lines)


def write_comicinfo(path, manga: dict, chapter: dict | None = None) -> None:
    """Writes ComicInfo.xml to `path` from the manga/chapter metadata."""
    from pathlib import Path

    path = Path(path)

    # aceita tanto dicts "nativos" (aidoku) quanto os do suwayomi
    title = (
        chapter.get("title")
        if chapter and chapter.get("title")
        else (chapter.get("name") if chapter else None)
    ) or manga.get("title", "")
    authors = manga.get("authors") or []
    if not authors and manga.get("author"):
        authors = [manga["author"]]
    artists = manga.get("artists") or authors

    from datetime import datetime, timezone

    ts = (chapter or {}).get("dateUploaded") or (chapter or {}).get("uploadDate")
    year = month = day = None
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            year, month, day = dt.year, dt.month, dt.day
        except (ValueError, OSError, OverflowError):
            pass

    num = None
    if chapter:
        raw = chapter.get("chapterNumber")
        if raw is not None:
            num = f"{raw:g}"

    vol = None
    if chapter:
        raw = chapter.get("volumeNumber")
        if raw is not None:
            vol = f"{raw:g}"

    xml = comicinfo_xml(
        title=title,
        series=manga.get("title", ""),
        number=num,
        volume=vol,
        summary=manga.get("description"),
        writer=", ".join(authors) or None,
        penciller=", ".join(artists) or None,
        tags=manga.get("tags"),
        web=manga.get("url") or manga.get("id"),
        year=year,
        month=month,
        day=day,
    )
    path.write_text(xml, encoding="utf-8")
    log.info("ComicInfo.xml escrito (%s)", path)
