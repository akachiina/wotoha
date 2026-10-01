"""Postcard serialization format (subset used by the Aidoku next-SDK ABI).

Reference: https://docs.rs/postcard — varint (LEB128) lengths, zigzag for
signed ints, fixed-width LE for floats, 1-byte discriminants for Option/enum.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, TypeVar

T = TypeVar("T")


class PostcardError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

def write_varint(out: BinaryIO, value: int) -> None:
    """LEB128 varint (what postcard uses for u32/usize)."""
    if value < 0:
        raise PostcardError("varint needs non-negative int")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.write(bytes([byte | 0x80]))
        else:
            out.write(bytes([byte]))
            return


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise PostcardError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise PostcardError("varint too long")


def zigzag_encode(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def write_string(out: BinaryIO, s: str) -> None:
    b = s.encode("utf-8")
    write_varint(out, len(b))
    out.write(b)


def read_string(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = read_varint(data, pos)
    end = pos + length
    if end > len(data):
        raise PostcardError("truncated string")
    return data[pos:end].decode("utf-8"), end


# --------------------------------------------------------------------------- #
# generic values
# --------------------------------------------------------------------------- #

def enc(value: Any) -> bytes:
    """Encode a python value using the Aidoku mapping.

    str -> String; bool -> bool; int -> i64 (zigzag varint); float -> f64;
    list -> Vec; None -> Option::None; PostcardStr -> raw String (str-encoded);
    dict -> struct-like via `enc_struct`.
    """
    import io

    out = io.BytesIO()
    _write_value(out, value)
    return out.getvalue()


def _write_value(out: BinaryIO, value: Any) -> None:
    if isinstance(value, PostcardStr):
        write_string(out, value.value)
    elif isinstance(value, bool):
        out.write(b"\x01" if value else b"\x00")
    elif isinstance(value, int):
        write_varint(out, zigzag_encode(value))
    elif isinstance(value, float):
        out.write(struct.pack("<d", value))
    elif isinstance(value, str):
        write_string(out, value)
    elif value is None:
        out.write(b"\x00")
    elif isinstance(value, PostcardSome):
        out.write(b"\x01")
        _write_value(out, value.value)
    elif isinstance(value, list):
        write_varint(out, len(value))
        for item in value:
            _write_value(out, item)
    elif isinstance(value, PostcardEnum):
        write_varint(out, value.variant)
        for arg in value.args:
            _write_value(out, arg)
    else:
        raise PostcardError(f"cannot encode {type(value)}")


class PostcardStr:
    """Marks a value to encode exactly as a postcard String (not option-wrapped)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class PostcardSome:
    """Explicitly encodes Option::Some(value)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class PostcardEnum:
    """Encodes an enum variant: varint discriminant + args."""

    __slots__ = ("variant", "args")

    def __init__(self, variant: int, *args: Any) -> None:
        self.variant = variant
        self.args = args


@dataclass
class Decoder:
    data: bytes
    pos: int = 0

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise PostcardError(f"truncated: need {n} bytes at {self.pos}")

    # -- primitives -- #
    def varint(self) -> int:
        v, self.pos = read_varint(self.data, self.pos)
        return v

    def zigzag(self) -> int:
        return zigzag_decode(self.varint())

    def i64(self) -> int:
        return self.zigzag()

    def f32(self) -> float:
        self._need(4)
        v = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f64(self) -> float:
        self._need(8)
        v = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        return v

    def byte(self) -> int:
        self._need(1)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def boolean(self) -> bool:
        return self.byte() != 0

    def string(self) -> str:
        s, self.pos = read_string(self.data, self.pos)
        return s

    def raw(self, n: int) -> bytes:
        self._need(n)
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def remaining(self) -> bytes:
        b = self.data[self.pos :]
        self.pos = len(self.data)
        return b

    # -- composites -- #
    def option(self, reader: Callable[["Decoder"], T]) -> T | None:
        if self.byte() == 0:
            return None
        return reader(self)

    def vec(self, reader: Callable[["Decoder"], T]) -> list[T]:
        n = self.varint()
        return [reader(self) for _ in range(n)]

    def map_str(self) -> dict[str, str]:
        n = self.varint()
        result = {}
        for _ in range(n):
            k = self.string()
            v = self.string()
            result[k] = v
        return result
