"""Aidoku next-SDK data models with postcard codecs.

Field order MUST match aidoku-rs structs (postcard is field-order sensitive).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Callable

from .postcard import Decoder, write_varint


# --------------------------------------------------------------------------- #
# low-level write helpers
# --------------------------------------------------------------------------- #

def w_str(out: io.BytesIO, s: str) -> None:
    b = s.encode("utf-8")
    write_varint(out, len(b))
    out.write(b)


def w_opt(out: io.BytesIO, value: Any, writer: Callable) -> None:
    if value is None:
        out.write(b"\x00")
    else:
        out.write(b"\x01")
        writer(out, value)


def w_opt_f32(out: io.BytesIO, value: float | None) -> None:
    import struct

    if value is None:
        out.write(b"\x00")
    else:
        out.write(b"\x01")
        out.write(struct.pack("<f", value))


def w_opt_i64(out: io.BytesIO, value: int | None) -> None:
    from .postcard import zigzag_encode

    if value is None:
        out.write(b"\x00")
    else:
        out.write(b"\x01")
        write_varint(out, zigzag_encode(value))


def w_str_list(out: io.BytesIO, items: list[str] | None) -> None:
    def _w(o, v):
        write_varint(o, len(v))
        for s in v:
            w_str(o, s)

    w_opt(out, items, _w)


def w_enum(out: io.BytesIO, discriminant: int) -> None:
    write_varint(out, discriminant)


def w_bool(out: io.BytesIO, value: bool) -> None:
    out.write(b"\x01" if value else b"\x00")


# --------------------------------------------------------------------------- #
# Chapter
# --------------------------------------------------------------------------- #

@dataclass
class Chapter:
    key: str
    title: str | None = None
    chapter_number: float | None = None
    volume_number: float | None = None
    date_uploaded: int | None = None
    scanlators: list[str] | None = None
    url: str | None = None
    language: str | None = None
    thumbnail: str | None = None
    locked: bool = False

    @staticmethod
    def decode(d: Decoder) -> "Chapter":
        return Chapter(
            key=d.string(),
            title=d.option(Decoder.string),
            chapter_number=d.option(Decoder.f32),
            volume_number=d.option(Decoder.f32),
            date_uploaded=d.option(Decoder.i64),
            scanlators=d.option(lambda dd: dd.vec(Decoder.string)),
            url=d.option(Decoder.string),
            language=d.option(Decoder.string),
            thumbnail=d.option(Decoder.string),
            locked=d.boolean(),
        )

    def encode(self) -> bytes:
        out = io.BytesIO()
        w_str(out, self.key)
        w_opt(out, self.title, w_str)
        w_opt_f32(out, self.chapter_number)
        w_opt_f32(out, self.volume_number)
        w_opt_i64(out, self.date_uploaded)
        w_str_list(out, self.scanlators)
        w_opt(out, self.url, w_str)
        w_opt(out, self.language, w_str)
        w_opt(out, self.thumbnail, w_str)
        w_bool(out, self.locked)
        return out.getvalue()

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "chapterNumber": self.chapter_number,
            "volumeNumber": self.volume_number,
            "dateUploaded": self.date_uploaded,
            "scanlators": self.scanlators,
            "url": self.url,
            "language": self.language,
            "locked": self.locked,
        }


# --------------------------------------------------------------------------- #
# Manga
# --------------------------------------------------------------------------- #

STATUS_NAMES = ["Unknown", "Ongoing", "Completed", "Cancelled", "Hiatus"]


@dataclass
class Manga:
    key: str = ""
    title: str = ""
    cover: str | None = None
    artists: list[str] | None = None
    authors: list[str] | None = None
    description: str | None = None
    url: str | None = None
    tags: list[str] | None = None
    status: int = 0
    content_rating: int = 0
    viewer: int = 0
    update_strategy: int = 0
    next_update_time: int | None = None
    chapters: list[Chapter] | None = None

    @staticmethod
    def decode(d: Decoder) -> "Manga":
        m = Manga(
            key=d.string(),
            title=d.string(),
            cover=d.option(Decoder.string),
            artists=d.option(lambda dd: dd.vec(Decoder.string)),
            authors=d.option(lambda dd: dd.vec(Decoder.string)),
            description=d.option(Decoder.string),
            url=d.option(Decoder.string),
            tags=d.option(lambda dd: dd.vec(Decoder.string)),
            status=d.varint(),
            content_rating=d.varint(),
            viewer=d.varint(),
            update_strategy=d.varint(),
            next_update_time=d.option(Decoder.i64),
        )
        m.chapters = d.option(lambda dd: dd.vec(Chapter.decode))
        return m

    def encode(self) -> bytes:
        out = io.BytesIO()
        w_str(out, self.key)
        w_str(out, self.title)
        w_opt(out, self.cover, w_str)
        w_str_list(out, self.artists)
        w_str_list(out, self.authors)
        w_opt(out, self.description, w_str)
        w_opt(out, self.url, w_str)
        w_str_list(out, self.tags)
        w_enum(out, self.status)
        w_enum(out, self.content_rating)
        w_enum(out, self.viewer)
        w_enum(out, self.update_strategy)
        w_opt_i64(out, self.next_update_time)
        if self.chapters is None:
            out.write(b"\x00")
        else:
            out.write(b"\x01")
            write_varint(out, len(self.chapters))
            for c in self.chapters:
                out.write(c.encode())
        return out.getvalue()

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "cover": self.cover,
            "artists": self.artists,
            "authors": self.authors,
            "description": self.description,
            "url": self.url,
            "tags": self.tags,
            "status": STATUS_NAMES[self.status] if self.status < len(STATUS_NAMES) else "Unknown",
            "viewer": self.viewer,
            "contentRating": self.content_rating,
        }


# --------------------------------------------------------------------------- #
# results / pages
# --------------------------------------------------------------------------- #

@dataclass
class MangaPageResult:
    entries: list[Manga]
    has_next_page: bool

    @staticmethod
    def decode(d: Decoder) -> "MangaPageResult":
        return MangaPageResult(entries=d.vec(Manga.decode), has_next_page=d.boolean())


@dataclass
class Page:
    content: Any  # ("url", url, context) | ("text", text) | ("image", ref) | ("zip", a, b)
    thumbnail: str | None = None
    has_description: bool = False
    description: str | None = None

    @staticmethod
    def decode(d: Decoder) -> "Page":
        tag = d.varint()
        if tag == 0:
            url = d.string()
            ctx = d.option(Decoder.map_str)
            content = ("url", url, ctx)
        elif tag == 1:
            content = ("text", d.string())
        elif tag == 2:
            h = d.raw(32)
            data = d.option(lambda dd: dd.vec(Decoder.byte))
            ext = d.boolean()
            content = ("image", {"hash": h.hex(), "data": data, "external": ext})
        elif tag == 3:
            content = ("zip", d.string(), d.string())
        else:
            raise ValueError(f"unknown PageContent tag {tag}")
        return Page(
            content=content,
            thumbnail=d.option(Decoder.string),
            has_description=d.boolean(),
            description=d.option(Decoder.string),
        )

    def image_url(self) -> str | None:
        if self.content and self.content[0] == "url":
            return self.content[1]
        return None


@dataclass
class Listing:
    id: str
    name: str
    kind: int = 0

    @staticmethod
    def decode(d: Decoder) -> "Listing":
        return Listing(id=d.string(), name=d.string(), kind=d.varint())


def decode_manga_page_result(data: bytes) -> MangaPageResult:
    return MangaPageResult.decode(Decoder(data))


def decode_manga(data: bytes) -> Manga:
    return Manga.decode(Decoder(data))


def decode_manga_list(data: bytes) -> list[Manga]:
    return Decoder(data).vec(Manga.decode)


def decode_chapter_list(data: bytes) -> list[Chapter]:
    return Decoder(data).vec(Chapter.decode)


def decode_page_list(data: bytes) -> list[Page]:
    return Decoder(data).vec(Page.decode)


def decode_listing_list(data: bytes) -> list[Listing]:
    return Decoder(data).vec(Listing.decode)
