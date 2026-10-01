"""Aidoku next-SDK WASM runtime for wotoha.

Host side of the ABI used by .aix sources (next-SDK 0.7+), mirroring
rakuyomi's backend (wasmi) with wasmtime-py.

ABI summary (derived from rakuyomi + aidoku-rs):
- Host-side handles are 1-based indices into tables; 0/-1 are errors.
- Strings pass as (ptr, len) into WASM linear memory.
- Source returns results as a pointer to [len:i32][cap:i32][postcard bytes];
  the host reads it and then calls the `free_result` export.
- std.read_buffer writes the postcard bytes of a stored handle value into
  WASM memory. Values stored with str_encode=True encode as postcard String
  (varint len + utf8). Non-marked strings encode as raw utf8 bytes.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any, Callable

import requests
from wasmtime import Engine, Func, FuncType, Instance, Linker, Memory, Module, Store, ValType

from . import models
from .net import apply_network_options, request_with_limit
from .postcard import Decoder, write_varint

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# html error codes shared with the source side
HTML_INVALID_DESCRIPTOR = -1
HTML_NO_RESULT = -5


class AidokuSourceError(Exception):
    pass


class AidokuRuntime:
    """One booted WASM source (one .aix package)."""

    def __init__(self, aix_path: str | Path) -> None:
        self.aix_path = Path(aix_path)
        self._load_package()
        self._boot()

    # ------------------------------------------------------------------ #
    # package
    # ------------------------------------------------------------------ #

    def _load_package(self) -> None:
        import json
        import zipfile

        self._default_settings: dict[str, Any] = {}
        with zipfile.ZipFile(self.aix_path) as zf:
            self.wasm_bytes = zf.read("Payload/main.wasm")
            self.source_json = self._read_json(zf, "Payload/source.json") or {}
            self.filters_json = self._read_json(zf, "Payload/filters.json") or []
            self.settings_json = self._read_json(zf, "Payload/settings.json") or []

        info = self.source_json.get("info") or {}
        self.id: str = info.get("id") or self.aix_path.stem
        self.name: str = info.get("name") or self.id
        self.lang: str = ",".join(info.get("languages") or [""])
        self.base_url: str = info.get("url") or ""
        self.version = info.get("version")

        # defaults declarados no settings.json do source
        # (itens ficam aninhados dentro de grupos {"items": [...]})
        def _walk(items):
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if item.get("key") and "default" in item:
                    self._default_settings[item["key"]] = item["default"]
                if isinstance(item.get("items"), list):
                    _walk(item["items"])

        _walk(self.settings_json)

    @staticmethod
    def _read_json(zf: zipfile.ZipFile, name: str) -> Any:
        try:
            import json

            return json.loads(zf.read(name))
        except KeyError:
            return None

    # ------------------------------------------------------------------ #
    # boot
    # ------------------------------------------------------------------ #

    def _boot(self) -> None:
        self.engine = Engine()
        self.store = Store(self.engine)
        self.module = Module(self.engine, self.wasm_bytes)

        # host tables ---------------------------------------------------- #
        self._std_values: dict[int, Any] = {}
        self._std_next: int = 1
        self._str_encoded: set[int] = set()
        self._requests: dict[int, dict[str, Any]] = {}
        self._req_next: int = 1
        self._elements: dict[int, dict[str, Any]] = {}
        self._el_next: int = 1
        self._settings: dict[str, Any] = {}
        self._http = apply_network_options(requests.Session())
        self._http.headers["User-Agent"] = DEFAULT_USER_AGENT
        # canvas: tabelas de contexts (PIL Image + transform) e images
        self._canvases: dict[int, dict[str, Any]] = {}
        self._canvas_next: int = 1
        self._images: dict[int, Any] = {}
        self._img_next: int = 1

        self.linker = Linker(self.engine)
        self._register_imports()
        self.instance: Instance = self.linker.instantiate(self.store, self.module)
        self.exports = self.instance.exports(self.store)
        self.memory: Memory = self.exports["memory"]

        start = self.exports.get("start")
        if isinstance(start, Func):
            start(self.store)

    def _export(self, name: str) -> Any:
        exp = self.exports.get(name)
        if exp is None:
            raise AidokuSourceError(f"export '{name}' not found in source")
        return exp

    # ------------------------------------------------------------------ #
    # memory helpers
    # ------------------------------------------------------------------ #

    def _mem_read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        size = self.memory.data_len(self.store)
        data = self.memory.read(self.store, offset, offset + length)
        if data is None or len(data) != length:
            raise AidokuSourceError(
                f"memory read failed at {offset}+{length} (mem size {size})"
            )
        # wasmtime-py retorna bytearray; normalizar para bytes (isinstance
        # checks e bs4 quebram com bytearray)
        if type(data) is not bytes:
            data = bytes(data)
        return data

    def _mem_write(self, offset: int, data: bytes) -> None:
        written = self.memory.write(self.store, data, offset)
        if written != len(data):
            raise AidokuSourceError(f"memory write failed at {offset}+{len(data)}")

    def _read_result_raw(self, ptr: int) -> bytes:
        """Read a [len:i32][cap:i32][bytes] result buffer returned by the source."""
        if ptr < 0:
            if ptr == -2:
                raise AidokuSourceError("Unimplemented (source returned -2)")
            if ptr == -3:
                raise AidokuSourceError("RequestError (source returned -3)")
            raise AidokuSourceError(f"source returned error pointer {ptr}")
        header = self._mem_read(ptr, 12)
        tag = int.from_bytes(header[0:4], "little", signed=True)
        length = int.from_bytes(header[4:8], "little", signed=True)
        if tag == -1:
            msg = self._mem_read(ptr + 12, length).decode("utf-8", "replace")
            raise AidokuSourceError(f"source error: {msg}")
        payload = self._mem_read(ptr + 8, length)
        self._free_result(ptr)
        return payload

    def _read_result_nofree(self, ptr: int) -> bytes:
        """Reads a [len][cap][bytes] buffer without freeing it (for buffers
        passed as arguments by the source, which own them)."""
        header = self._mem_read(ptr, 12)
        tag = int.from_bytes(header[0:4], "little", signed=True)
        length = int.from_bytes(header[4:8], "little", signed=True)
        if tag == -1:
            msg = self._mem_read(ptr + 12, length).decode("utf-8", "replace")
            raise AidokuSourceError(f"source error: {msg}")
        return self._mem_read(ptr + 8, length)

    def _free_result(self, ptr: int) -> None:
        try:
            free_fn = self.exports.get("free_result")
            if isinstance(free_fn, Func):
                free_fn(self.store, ptr)
        except Exception:  # noqa: BLE001
            log.debug("free_result failed for ptr=%s", ptr)

    def _call_ptr(self, func_name: str, args: tuple) -> bytes:
        fn = self._export(func_name)
        ptr = fn(self.store, *args)
        return self._read_result_raw(ptr)

    # ------------------------------------------------------------------ #
    # std value store
    # ------------------------------------------------------------------ #

    def _store_value(self, value: Any, str_encode: bool = False) -> int:
        handle = self._std_next
        self._std_next += 1
        self._std_values[handle] = value
        if str_encode:
            self._str_encoded.add(handle)
        return handle

    def _std_destroy(self, handle: int) -> None:
        self._std_values.pop(handle, None)
        self._str_encoded.discard(handle)

    def _read_buffer_bytes(self, handle: int) -> bytes:
        value = self._std_values.get(handle)
        if value is None:
            raise AidokuSourceError(f"invalid std handle {handle}")
        out = io.BytesIO()
        if isinstance(value, str):
            b = value.encode("utf-8")
            if handle in self._str_encoded:
                write_varint(out, len(b))
            out.write(b)
        elif isinstance(value, bool):
            out.write(b"\x01" if value else b"\x00")
        elif isinstance(value, int):
            from .postcard import zigzag_encode

            write_varint(out, zigzag_encode(value))
        elif isinstance(value, float):
            import struct

            out.write(struct.pack("<d", value))
        elif isinstance(value, bytes):
            out.write(value)
        elif isinstance(value, list):
            write_varint(out, len(value))
            for item in value:
                out.write(self._serialize_list_item(item))
        elif isinstance(value, models.Manga):
            out.write(value.encode())
        elif isinstance(value, models.Chapter):
            out.write(value.encode())
        else:
            raise AidokuSourceError(f"unsupported read_buffer type {type(value)}")
        return out.getvalue()

    def _serialize_list_item(self, item: Any) -> bytes:
        w = io.BytesIO()
        if isinstance(item, str):
            write_varint(w, len(item.encode()))
            w.write(item.encode())
        elif isinstance(item, bool):
            w.write(b"\x01" if item else b"\x00")
        elif isinstance(item, int):
            from .postcard import zigzag_encode

            write_varint(w, zigzag_encode(item))
        elif isinstance(item, float):
            import struct

            w.write(struct.pack("<d", item))
        else:
            raise AidokuSourceError(f"unsupported list item {type(item)}")
        return w.getvalue()

    # ------------------------------------------------------------------ #
    # import registration
    # ------------------------------------------------------------------ #

    # set WOTOHA_TRACE=1 to log every host call
    TRACE = __import__("os").environ.get("WOTOHA_TRACE") == "1"

    def _register_imports(self) -> None:
        store = self.store
        i32 = ValType.i32()
        f32 = ValType.f32()
        f64 = ValType.f64()

        def add(module: str, name: str, params, results, func) -> None:
            if self.TRACE:
                def wrapped(*args):
                    result = func(*args)
                    log.info("[%s.%s] %s -> %s", module, name, args, result)
                    return result
                fn = Func(store, FuncType(params, results), wrapped)
            else:
                fn = Func(store, FuncType(params, results), func)
            self.linker.define(store, module, name, fn)

        # std ------------------------------------------------------------ #
        add("std", "destroy", [i32], [], self._h_std_destroy)
        add("std", "buffer_len", [i32], [i32], self._h_std_buffer_len)
        add("std", "read_buffer", [i32, i32, i32], [i32], self._h_std_read_buffer)
        add("std", "print", [i32, i32], [], self._h_std_print)
        add("std", "abort", [], [], self._h_std_abort)
        add("std", "current_date", [], [f64], self._h_std_current_date)
        add("std", "utc_offset", [], [f64], self._h_std_utc_offset)
        add("std", "parse_date", [i32] * 8, [f64], self._h_std_parse_date)

        # env ------------------------------------------------------------ #
        add("env", "print", [i32, i32], [], self._h_std_print)
        # abort do Rust compilado p/ wasm (panic path em sources 0.8, ex. webtoon)
        add("env", "abort", [], [], self._h_std_abort)
        # anti-rate-limit pause requested by the source (capped so it can't hang the CLI)
        add("env", "sleep", [i32], [], self._h_env_sleep)

        # net ------------------------------------------------------------ #
        add("net", "init", [i32], [i32], self._h_net_init)
        add("net", "set_url", [i32, i32, i32], [i32], self._h_net_set_url)
        add("net", "set_header", [i32] * 5, [i32], self._h_net_set_header)
        add("net", "set_body", [i32, i32, i32], [i32], self._h_net_set_body)
        add("net", "send", [i32], [i32], self._h_net_send)
        add("net", "send_all", [i32, i32], [i32], self._h_net_send_all)
        add("net", "data_len", [i32], [i32], self._h_net_data_len)
        add("net", "read_data", [i32, i32, i32], [i32], self._h_net_read_data)
        add("net", "get_url", [i32], [i32], self._h_net_get_url)
        add("net", "get_status_code", [i32], [i32], self._h_net_get_status_code)
        add("net", "get_header", [i32, i32, i32], [i32], self._h_net_get_header)
        add("net", "html", [i32], [i32], self._h_net_html)
        add("net", "get_image", [i32], [i32], self._h_net_get_image)
        add("net", "set_rate_limit", [i32, i32, i32], [], self._h_net_set_rate_limit)
        add("net", "set_timeout", [i32, f64], [i32], self._h_net_set_timeout)

        # html ----------------------------------------------------------- #
        add("html", "parse", [i32] * 4, [i32], self._h_html_parse)
        add("html", "parse_fragment", [i32] * 4, [i32], self._h_html_parse_fragment)
        add("html", "select", [i32, i32, i32], [i32], self._h_html_select)
        add("html", "select_first", [i32, i32, i32], [i32], self._h_html_select_first)
        add("html", "attr", [i32, i32, i32], [i32], self._h_html_attr)
        add("html", "text", [i32], [i32], self._h_html_text)
        add("html", "own_text", [i32], [i32], self._h_html_own_text)
        add("html", "get", [i32, i32], [i32], self._h_html_get)
        add("html", "size", [i32], [i32], self._h_html_size)

        # html: mutation
        add("html", "set_text", [i32] * 3, [i32], self._h_html_set_text)
        add("html", "set_html", [i32] * 3, [i32], self._h_html_set_html)
        add("html", "prepend", [i32] * 3, [i32], self._h_html_prepend)
        add("html", "append", [i32] * 3, [i32], self._h_html_append)
        add("html", "remove", [i32], [i32], self._h_html_remove)
        add("html", "set_attr", [i32] * 5, [i32], self._h_html_set_attr)
        add("html", "remove_attr", [i32] * 3, [i32], self._h_html_remove_attr)
        add("html", "add_class", [i32] * 3, [i32], self._h_html_add_class)
        add("html", "remove_class", [i32] * 3, [i32], self._h_html_remove_class)

        # html: serialization/query
        add("html", "html", [i32], [i32], self._h_html_html)
        add("html", "outer_html", [i32], [i32], self._h_html_outer_html)
        add("html", "untrimmed_text", [i32], [i32], self._h_html_text)
        add("html", "data", [i32], [i32], self._h_html_data)
        add("html", "base_uri", [i32], [i32], self._h_html_base_uri)
        add("html", "id", [i32], [i32], self._h_html_id)
        add("html", "tag_name", [i32], [i32], self._h_html_tag_name)
        add("html", "class_name", [i32], [i32], self._h_html_class_name)
        add("html", "has_class", [i32] * 3, [i32], self._h_html_has_class)
        add("html", "has_attr", [i32] * 3, [i32], self._h_html_has_attr)
        add("html", "escape", [i32, i32], [i32], self._h_html_escape)
        add("html", "unescape", [i32, i32], [i32], self._h_html_unescape)

        # html: travessia
        add("html", "first", [i32], [i32], self._h_html_first)
        add("html", "last", [i32], [i32], self._h_html_last)
        add("html", "parent", [i32], [i32], self._h_html_parent)
        add("html", "children", [i32], [i32], self._h_html_children)
        add("html", "child_nodes", [i32], [i32], self._h_html_child_nodes)
        add("html", "siblings", [i32], [i32], self._h_html_siblings)
        add("html", "next", [i32], [i32], self._h_html_next)
        add("html", "previous", [i32], [i32], self._h_html_previous)
        add("html", "kind", [i32], [i32], self._h_html_kind)

        # env ------------------------------------------------------------ #
        # source sends a partial result (streaming for UI); no-op in the CLI
        add("env", "send_partial_result", [i32], [], lambda handle: None)

        # canvas --------------------------------------------------------- #
        # 2D surface for image descrambling (MangaPlus, MangaDex...).
        # Implemented with Pillow; fill/stroke/draw_text are safe no-ops.
        add("canvas", "new_context", [f32, f32], [i32], self._h_canvas_new_context)
        add("canvas", "set_transform", [i32, f32, f32, f32, f32, f32], [i32], self._h_canvas_set_transform)
        add("canvas", "draw_image", [i32, i32, f32, f32, f32, f32], [i32], self._h_canvas_draw_image)
        add("canvas", "copy_image", [i32, i32] + [f32] * 8, [i32], self._h_canvas_copy_image)
        add("canvas", "fill", [i32, i32, f32, f32, f32, f32], [i32], self._h_canvas_noop_ok)
        add("canvas", "stroke", [i32, i32, i32], [i32], self._h_canvas_noop_ok)
        add("canvas", "draw_text", [i32, i32, i32, f32, f32, f32, i32, f32, f32, f32, f32], [i32], self._h_canvas_noop_ok)
        add("canvas", "new_font", [i32, i32], [i32], self._h_canvas_new_font)
        add("canvas", "system_font", [i32], [i32], self._h_canvas_new_font)
        add("canvas", "load_font", [i32, i32], [i32], self._h_canvas_new_font)
        add("canvas", "new_image", [i32, i32], [i32], self._h_canvas_new_image)
        add("canvas", "get_image", [i32], [i32], self._h_canvas_get_image)
        add("canvas", "get_image_data", [i32], [i32], self._h_canvas_get_image_data)
        add("canvas", "get_image_width", [i32], [f32], self._h_canvas_get_image_width)
        add("canvas", "get_image_height", [i32], [f32], self._h_canvas_get_image_height)

        # defaults ------------------------------------------------------- #
        add("defaults", "get", [i32, i32], [i32], self._h_defaults_get)
        add("defaults", "set", [i32, i32, i32, i32], [i32], self._h_defaults_set)

    # -- std handlers -------------------------------------------------- #

    def _h_std_destroy(self, ptr: int) -> None:
        self._std_destroy(ptr)

    def _h_std_buffer_len(self, ptr: int) -> int:
        return len(self._read_buffer_bytes(ptr))

    def _h_std_read_buffer(self, ptr: int, offset: int, size: int) -> int:
        data = self._read_buffer_bytes(ptr)
        if 0 < size <= len(data):
            self._mem_write(offset, data[:size])
        return 0

    def _h_std_print(self, ptr: int, length: int) -> None:
        if length > 0:
            msg = self._mem_read(ptr, length).decode("utf-8", "replace")
            log.info("[source] %s", msg)

    def _h_std_abort(self) -> None:
        raise AidokuSourceError("source aborted (std.abort)")

    def _h_env_sleep(self, seconds: int) -> None:
        cap = min(max(0, seconds), 10)
        if cap < seconds:
            log.debug("env.sleep(%ds) limitado a %ds", seconds, cap)
        if cap > 0:
            time.sleep(cap)

    def _h_std_current_date(self) -> float:
        return time.time()

    def _h_std_utc_offset(self) -> float:
        return -(time.localtime().tm_gmtoff or 0)

    def _h_std_parse_date(self, ptr: int, length: int, *_args) -> float:
        try:
            s = self._mem_read(ptr, length).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return 0.0
        from datetime import datetime, timezone

        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y",
            "%d %b %Y", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S%z", "%B %d, %Y",
        ):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        return 0.0

    # -- net handlers -------------------------------------------------- #

    def _h_net_init(self, method: int) -> int:
        methods = ["GET", "POST", "PUT", "HEAD", "DELETE", "PATCH", "OPTIONS"]
        req = {
            "method": methods[method] if 0 <= method < len(methods) else "GET",
            "url": None,
            "headers": {"User-Agent": DEFAULT_USER_AGENT},
            "body": None,
            "timeout": None,
            "response": None,
        }
        handle = self._req_next
        self._req_next += 1
        self._requests[handle] = req
        return handle

    def _h_net_set_url(self, handle: int, ptr: int, length: int) -> int:
        self._requests[handle]["url"] = self._mem_read(ptr, length).decode()
        return 0

    def _h_net_set_header(self, handle: int, kptr: int, klen: int, vptr: int, vlen: int) -> int:
        self._requests[handle]["headers"][self._mem_read(kptr, klen).decode()] = (
            self._mem_read(vptr, vlen).decode()
        )
        return 0

    def _h_net_set_body(self, handle: int, ptr: int, length: int) -> int:
        self._requests[handle]["body"] = self._mem_read(ptr, length)
        return 0

    def _h_net_send(self, handle: int) -> int:
        self._do_request(handle)
        return 0

    def _h_net_send_all(self, rd: int, length: int) -> int:
        buf = self._mem_read(rd, 4 * length)
        for i in range(length):
            handle = int.from_bytes(buf[i * 4 : i * 4 + 4], "little", signed=True)
            self._do_request(handle)
        return 0

    def _do_request(self, handle: int) -> None:
        req = self._requests[handle]
        if not req["url"]:
            raise AidokuSourceError("request has no url")
        try:
            resp = request_with_limit(
                self._http,
                req["method"],
                req["url"],
                headers=req["headers"],
                data=req["body"],
                timeout=req["timeout"] or None,
            )
        except Exception as e:  # noqa: BLE001
            raise AidokuSourceError(f"request failed: {e}") from e
        body = resp.content
        body = self._normalize_mangaplus_directory(req["url"], body)
        req["response"] = {
            "status": resp.status_code,
            "headers": {k.lower(): v for k, v in resp.headers.items()},
            "body": body,
            "url": str(resp.url),
        }

    # -- protobuf normalization (MangaPlus) ------------------------------ #

    @staticmethod
    def _pb_split(buf: bytes) -> list[tuple[int, int, bytes]]:
        """Divide uma mensagem protobuf em (tag, wire, payload) preservando ordem."""
        out: list[tuple[int, int, bytes]] = []
        i, n = 0, len(buf)
        while i < n:
            key = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                key |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            tag, wire = key >> 3, key & 7
            if wire == 0:
                start = i
                while buf[i] & 0x80:
                    i += 1
                i += 1
                out.append((tag, wire, buf[start:i]))
            elif wire == 1:
                out.append((tag, wire, buf[i:i + 8]))
                i += 8
            elif wire == 5:
                out.append((tag, wire, buf[i:i + 4]))
                i += 4
            elif wire == 2:
                ln = 0
                shift = 0
                while True:
                    b = buf[i]
                    i += 1
                    ln |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                out.append((tag, wire, buf[i:i + ln]))
                i += ln
            else:
                raise ValueError(f"wire type {wire}")
        return out

    @staticmethod
    def _pb_join(fields: list[tuple[int, int, bytes]]) -> bytes:
        """Re-serializa (tag, wire, payload) em bytes protobuf."""
        out = bytearray()
        for tag, wire, payload in fields:
            v = (tag << 3) | wire
            while True:
                b = v & 0x7F
                v >>= 7
                if v:
                    out.append(b | 0x80)
                else:
                    out.append(b)
                    break
            if wire == 2:
                ln = len(payload)
                while True:
                    b = ln & 0x7F
                    ln >>= 7
                    if ln:
                        out.append(b | 0x80)
                    else:
                        out.append(b)
                        break
            out += payload
        return bytes(out)

    def _normalize_mangaplus_directory(self, url: str, body: bytes) -> bytes:
        """The MangaPlus title_list/all_v3 API started sending multiple
        language variants per entry, but the source declares `title` as
        singular; prost merges them and the last variant with a defined
        language wins, dropping popular titles from the EN catalog (One
        Piece etc.). Normalization: keep only the 1st variant per entry."""
        try:
            if "jumpg-" not in url or "title_list/all_v3" not in url:
                return body
            top = self._pb_split(body)
            changed = False
            for i1, (tag1, wire1, success) in enumerate(top):
                if tag1 != 1 or wire1 != 2:
                    continue
                sf = self._pb_split(success)
                for i2, (tag2, wire2, allv3) in enumerate(sf):
                    if tag2 != 35 or wire2 != 2:
                        continue
                    v3f = self._pb_split(allv3)
                    new_v3f = []
                    mod = False
                    for tag3, wire3, entry in v3f:
                        if tag3 == 3 and wire3 == 2:
                            ef = self._pb_split(entry)
                            titles = [k for k, f in enumerate(ef) if f[0] == 2 and f[1] == 2]
                            if len(titles) > 1:
                                # drop extra variants (keep the 1st)
                                keep = {titles[0]} | {
                                    k for k, f in enumerate(ef) if not (f[0] == 2 and f[1] == 2)
                                }
                                new_v3f.append((tag3, wire3, self._pb_join([f for k, f in enumerate(ef) if k in keep])))
                                mod = True
                                continue
                        new_v3f.append((tag3, wire3, entry))
                    if mod:
                        sf[i2] = (35, 2, self._pb_join(new_v3f))
                        top[i1] = (1, 2, self._pb_join(sf))
                        changed = True
                    break
                if changed:
                    break
            if changed:
                new_body = self._pb_join(top)
                log.debug(
                    "mangaplus: directory normalized (%d -> %d bytes)",
                    len(body), len(new_body),
                )
                return new_body
        except Exception as e:  # noqa: BLE001
            log.debug("normalize mangaplus falhou: %s", e)
        return body

    def _h_net_data_len(self, handle: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp or resp["body"] is None:
            return -1
        return len(resp["body"])

    def _h_net_read_data(self, handle: int, offset: int, size: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp or resp["body"] is None:
            return -1
        self._mem_write(offset, resp["body"][:size])
        return 0

    def _h_net_get_url(self, handle: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp:
            return -1
        return self._store_value(resp["url"], str_encode=False)

    def _h_net_get_status_code(self, handle: int) -> int:
        resp = self._requests[handle].get("response")
        return resp["status"] if resp else -1

    def _h_net_get_header(self, handle: int, kptr: int, klen: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp:
            return -1
        key = self._mem_read(kptr, klen).decode().lower()
        value = resp["headers"].get(key)
        if value is None:
            return -1
        return self._store_value(value, str_encode=False)

    def _h_net_html(self, handle: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp or resp["body"] is None:
            return -1
        return self._html_parse(resp["body"], resp["url"] or "")

    def _h_net_get_image(self, handle: int) -> int:
        resp = self._requests[handle].get("response")
        if not resp or not resp["body"]:
            return -1
        # ImageRef minimal: only used by sources embedding images; store raw bytes
        return -1  # not-an-image for now (sources embedding raw images are rare)

    def _h_net_set_rate_limit(self, permits: int, period: int, unit: int) -> None:
        # sources may request their own limit; the user's global always wins
        mult = {0: 1, 1: 60, 2: 3600}.get(unit, 1)
        log.debug("source rate limit: %d per %ds (global vence)", permits, period * mult)

    def _h_net_set_timeout(self, handle: int, value: float) -> int:
        self._requests[handle]["timeout"] = value
        return 0

    # -- html handlers ------------------------------------------------- #

    def _h_html_parse(self, html_ptr: int, html_len: int, base_ptr: int, base_len: int) -> int:
        html = self._mem_read(html_ptr, html_len)
        base = self._mem_read(base_ptr, base_len).decode() if base_len > 0 else ""
        return self._html_parse(html, base)

    def _h_html_parse_fragment(self, html_ptr: int, html_len: int, base_ptr: int, base_len: int) -> int:
        return self._h_html_parse(html_ptr, html_len, base_ptr, base_len)

    def _html_parse(self, html: bytes, base_url: str) -> int:
        from bs4 import BeautifulSoup, UnicodeDammit

        # bs4 4.13 quebra com bytes misturados em warn_if_markup_looks_like_xml;
        # always decode to str (encoding detection via UnicodeDammit)
        if isinstance(html, (bytes, bytearray)):
            dammit = UnicodeDammit(html, ["utf-8", "windows-1252", "iso-8859-1"])
            html = dammit.unicode_markup or html.decode("utf-8", "replace")
        soup = BeautifulSoup(html, "lxml")
        handle = self._el_next
        self._el_next += 1
        self._elements[handle] = {"soup": soup, "node": soup, "base": base_url}
        return handle

    def _el(self, handle: int) -> dict[str, Any]:
        el = self._elements.get(handle)
        if el is None:
            raise AidokuSourceError(f"invalid element handle {handle}")
        return el

    def _store_element(self, soup, node, base: str) -> int:
        handle = self._el_next
        self._el_next += 1
        self._elements[handle] = {"soup": soup, "node": node, "base": base}
        return handle

    @staticmethod
    def _normalize_selector(query: str) -> str:
        """Quote unquoted attribute values (SwiftSoup/jsoup are lenient,
        soupsieve is not): a[href^=/browse?x] -> a[href^="/browse?x"]."""
        import re

        def _quote(m):
            name, op, value = m.group(1), m.group(2), m.group(3)
            if value[0] in "\"'":
                return m.group(0)
            return f'[{name}{op}"{value}"]'

        return re.sub(
            r"\[\s*([\w:-]+)\s*([*^$|~]?=)\s*([^\]\"'\s]+)\s*\]",
            _quote,
            query,
        )

    def _h_html_select(self, handle: int, qptr: int, qlen: int) -> int:
        el = self._el(handle)
        query = self._normalize_selector(self._mem_read(qptr, qlen).decode())
        nodes = el["node"].select(query)
        if self.TRACE:
            log.info("[html.select] doc=%s q=%r -> %d nodes", handle, query, len(nodes))
        # mesmo com 0 matches, retorna o handle (igual ao rakuyomi): o template
        # madara trata None como "Invalid chapter selector" e aborta antes de
        # tentar o fallback AJAX; com lista vazia ele tenta o POST /ajax/chapters
        return self._store_element(el["soup"], nodes, el["base"])

    def _h_html_select_first(self, handle: int, qptr: int, qlen: int) -> int:
        el = self._el(handle)
        query = self._normalize_selector(self._mem_read(qptr, qlen).decode())
        node = el["node"].select_one(query)
        if self.TRACE:
            log.info("[html.select_first] doc=%s q=%r -> %s", handle, query, "hit" if node else "MISS")
        if node is None:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], node, el["base"])

    def _h_html_text(self, handle: int) -> int:
        node = self._el(handle)["node"]
        if isinstance(node, list):
            text = " ".join(n.get_text() for n in node)
        else:
            text = node.get_text()
        return self._store_value(text, str_encode=False)

    def _h_html_own_text(self, handle: int) -> int:
        node = self._el(handle)["node"]
        if isinstance(node, list):
            parts = ["".join(n.find_all(string=True, recursive=False)) for n in node]
            text = " ".join(parts)
        else:
            text = "".join(node.find_all(string=True, recursive=False))
        return self._store_value(text, str_encode=False)

    def _h_html_attr(self, handle: int, kptr: int, klen: int) -> int:
        el = self._el(handle)
        key = self._mem_read(kptr, klen).decode()
        node = el["node"][0] if isinstance(el["node"], list) and el["node"] else el["node"]

        # SwiftSoup-style absolute URL pseudo-attributes (abs:href, abs:src)
        abs_prefix = ""
        if key.startswith("abs:"):
            abs_prefix = "abs:"
            key = key[4:]

        value = getattr(node, "attrs", {}).get(key)
        if value is None:
            return HTML_INVALID_DESCRIPTOR
        if isinstance(value, list):
            value = " ".join(value)
        value = str(value)

        if abs_prefix:
            from urllib.parse import urljoin

            value = urljoin(el.get("base") or "", value)
        return self._store_value(value, str_encode=False)

    def _h_html_get(self, handle: int, index: int) -> int:
        el = self._el(handle)
        if not isinstance(el["node"], list) or index >= len(el["node"]):
            return HTML_INVALID_DESCRIPTOR
        return self._store_element(el["soup"], el["node"][index], el["base"])

    def _h_html_size(self, handle: int) -> int:
        node = self._el(handle)["node"]
        return len(node) if isinstance(node, list) else 1

    # -- html: mutation ------------------------------------------------- #

    def _h_html_set_text(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        text = self._mem_read(ptr, length).decode("utf-8", "replace")
        targets = node if isinstance(node, list) else [node]
        for n in targets:
            if getattr(n, "string", None) is not None:
                n.string.replace_with(text)
            else:
                n.clear()
                n.append(text)
        return 0

    def _h_html_set_html(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        html = self._mem_read(ptr, length).decode("utf-8", "replace")
        targets = node if isinstance(node, list) else [node]
        for n in targets:
            n.clear()
            new = BeautifulSoup(html, "lxml")
            for child in list(new.children):
                n.append(child)
        return 0

    def _h_html_prepend(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        html = self._mem_read(ptr, length).decode("utf-8", "replace")
        targets = node if isinstance(node, list) else [node]
        for n in targets:
            new = BeautifulSoup(html, "lxml")
            for child in reversed(list(new.children)):
                n.insert(0, child)
        return 0

    def _h_html_append(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        html = self._mem_read(ptr, length).decode("utf-8", "replace")
        targets = node if isinstance(node, list) else [node]
        for n in targets:
            new = BeautifulSoup(html, "lxml")
            for child in list(new.children):
                n.append(child)
        return 0

    def _h_html_remove(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        for n in targets:
            if hasattr(n, "decompose"):
                n.decompose()
        return 0

    def _h_html_set_attr(self, handle: int, kptr: int, klen: int, vptr: int, vlen: int) -> int:
        node = self._el(handle)["node"]
        key = self._mem_read(kptr, klen).decode()
        value = self._mem_read(vptr, vlen).decode()
        targets = node if isinstance(node, list) else [node]
        first = targets[0] if targets else None
        if first is not None and hasattr(first, "attrs"):
            first.attrs[key] = value
        return 0

    def _h_html_remove_attr(self, handle: int, kptr: int, klen: int) -> int:
        node = self._el(handle)["node"]
        key = self._mem_read(kptr, klen).decode()
        targets = node if isinstance(node, list) else [node]
        first = targets[0] if targets else None
        if first is not None and hasattr(first, "attrs"):
            first.attrs.pop(key, None)
        return 0

    def _h_html_add_class(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        name = self._mem_read(ptr, length).decode()
        targets = node if isinstance(node, list) else [node]
        first = targets[0] if targets else None
        if first is not None:
            existing = first.get("class") or []
            if isinstance(existing, str):
                existing = existing.split()
            if name not in existing:
                first.attrs["class"] = existing + [name]
        return 0

    def _h_html_remove_class(self, handle: int, ptr: int, length: int) -> int:
        node = self._el(handle)["node"]
        name = self._mem_read(ptr, length).decode()
        targets = node if isinstance(node, list) else [node]
        first = targets[0] if targets else None
        if first is not None:
            existing = first.get("class") or []
            if isinstance(existing, str):
                existing = existing.split()
            first.attrs["class"] = [c for c in existing if c != name]
        return 0

    # -- html: serialization/query -------------------------------------- #

    def _h_html_html(self, handle: int) -> int:
        """innerHTML do primeiro elemento."""
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        if n is None:
            return HTML_NO_RESULT
        return self._store_value(n.decode_contents() if hasattr(n, "decode_contents") else str(n), str_encode=False)

    def _h_html_outer_html(self, handle: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        if n is None:
            return HTML_NO_RESULT
        return self._store_value(str(n), str_encode=False)

    def _h_html_data(self, handle: int) -> int:
        """Content of text/comment/data nodes."""
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        if n is None:
            return HTML_NO_RESULT
        return self._store_value(str(n), str_encode=False)

    def _h_html_base_uri(self, handle: int) -> int:
        return self._store_value(self._el(handle).get("base") or "", str_encode=False)

    def _h_html_id(self, handle: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        val = getattr(n, "attrs", {}).get("id")
        return self._store_value(str(val), str_encode=False) if val is not None else HTML_NO_RESULT

    def _h_html_tag_name(self, handle: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        if n is None or not hasattr(n, "name"):
            return HTML_NO_RESULT
        return self._store_value(n.name or "", str_encode=False)

    def _h_html_class_name(self, handle: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        val = getattr(n, "attrs", {}).get("class")
        if isinstance(val, list):
            val = " ".join(val)
        return self._store_value(str(val), str_encode=False) if val is not None else HTML_NO_RESULT

    def _h_html_has_class(self, handle: int, kptr: int, klen: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        name = self._mem_read(kptr, klen).decode()
        classes = n.get("class") or [] if hasattr(n, "get") else []
        if isinstance(classes, str):
            classes = classes.split()
        return 1 if name in classes else 0

    def _h_html_has_attr(self, handle: int, kptr: int, klen: int) -> int:
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        key = self._mem_read(kptr, klen).decode()
        return 1 if key in getattr(n, "attrs", {}) else 0

    def _h_html_escape(self, ptr: int, length: int) -> int:
        from xml.sax.saxutils import escape as xml_escape

        text = self._mem_read(ptr, length).decode("utf-8", "replace")
        return self._store_value(xml_escape(text), str_encode=False)

    def _h_html_unescape(self, ptr: int, length: int) -> int:
        import html as _html

        text = self._mem_read(ptr, length).decode("utf-8", "replace")
        return self._store_value(_html.unescape(text), str_encode=False)

    # -- html: travessia ------------------------------------------------- #

    def _h_html_first(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        if isinstance(node, list):
            if not node:
                return HTML_NO_RESULT
            return self._store_element(el["soup"], node[0], el["base"])
        return handle

    def _h_html_last(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        if isinstance(node, list):
            if not node:
                return HTML_NO_RESULT
            return self._store_element(el["soup"], node[-1], el["base"])
        return handle

    def _h_html_parent(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        parents = [n.parent for n in targets if getattr(n, "parent", None) is not None]
        if not parents:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], parents, el["base"])

    def _h_html_children(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        children = []
        for n in targets:
            if hasattr(n, "find_all"):
                children.extend(n.find_all(recursive=False))
        if not children:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], children, el["base"])

    def _h_html_child_nodes(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        kids = []
        for n in targets:
            kids.extend(c for c in n.children if getattr(c, "name", None) is not None)
        if not kids:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], kids, el["base"])

    def _h_html_siblings(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        sibs = []
        for n in targets:
            if getattr(n, "parent", None) is not None:
                sibs.extend(n.parent.find_all(recursive=False))
        if not sibs:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], sibs, el["base"])

    def _h_html_next(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        nexts = []
        for n in targets:
            s = getattr(n, "next_sibling", None)
            while s is not None and getattr(s, "name", None) is None:
                s = getattr(s, "next_sibling", None)
            if s is not None:
                nexts.append(s)
        if not nexts:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], nexts, el["base"])

    def _h_html_previous(self, handle: int) -> int:
        el = self._el(handle)
        node = el["node"]
        targets = node if isinstance(node, list) else [node]
        prevs = []
        for n in targets:
            s = getattr(n, "previous_sibling", None)
            while s is not None and getattr(s, "name", None) is None:
                s = getattr(s, "previous_sibling", None)
            if s is not None:
                prevs.append(s)
        if not prevs:
            return HTML_NO_RESULT
        return self._store_element(el["soup"], prevs, el["base"])

    def _h_html_kind(self, handle: int) -> int:
        """NodeType: 1=element, 3=text, 5=data... (keeps dom_query compat)."""
        node = self._el(handle)["node"]
        n = node[0] if isinstance(node, list) and node else node
        if n is None:
            return 0
        if getattr(n, "name", None) is None:
            return 3  # text node
        return 1  # element

    # -- defaults handlers ---------------------------------------------- #

    def set_setting(self, key: str, value: Any) -> None:
        self._settings[key] = value

    # ISO 639-2 codes (config) -> 639-1 (what Aidoku sources expect)
    _LANG_MAP = {
        "eng": "en", "jpn": "ja", "por": "pt", "spa": "es", "fra": "fr",
        "fre": "fr", "rus": "ru", "chi": "zh", "zho": "zh", "kor": "ko",
        "tha": "th", "vie": "vi", "ger": "de", "deu": "de", "ind": "id",
        "ita": "it", "pol": "pl", "tur": "tr", "ara": "ar", "hin": "hi",
    }

    @classmethod
    def _to_source_lang(cls, code: str) -> str:
        c = code.strip()
        return cls._LANG_MAP.get(c.lower(), c) if len(c) == 3 else c

    def _h_defaults_get(self, kptr: int, klen: int) -> int:
        key = self._mem_read(kptr, klen).decode()
        # caso especial do rakuyomi: idiomas configurados no app
        # (MangaPlus/MangaDex filter the catalog by it)
        if key == "languages":
            from .config import config

            langs = [l.strip() for l in config.languages.split(",") if l.strip()]
            langs = [self._to_source_lang(l) for l in (langs or ["eng"])]
            return self._store_value(langs, str_encode=True)
        if key in self._settings:
            return self._store_value(self._settings[key], str_encode=True)
        if key in self._default_settings:
            return self._store_value(self._default_settings[key], str_encode=True)
        return -2

    def _h_defaults_set(self, kptr: int, klen: int, kind: int, vptr: int) -> int:
        key = self._mem_read(kptr, klen).decode()
        try:
            # vptr is a buffer PASSED BY THE SOURCE (argument): the host only reads it;
            # freeing here caused double-free/memory traps on 0.8 sources
            payload = self._read_result_nofree(vptr) if vptr > 0 else b""
            d = Decoder(payload)
            reader = {
                1: Decoder.boolean,
                2: Decoder.i64,
                3: Decoder.f64,
                4: Decoder.string,
            }.get(kind, Decoder.string)
            self._settings[key] = reader(d)
        except Exception:  # noqa: BLE001
            self._settings[key] = None
        return 0

    # -- canvas handlers ------------------------------------------------- #

    def _h_canvas_new_context(self, width: float, height: float) -> int:
        from PIL import Image as PILImage

        w, h = max(1, int(width)), max(1, int(height))
        handle = self._canvas_next
        self._canvas_next += 1
        self._canvases[handle] = {
            "image": PILImage.new("RGBA", (w, h), (0, 0, 0, 0)),
            "transform": (0.0, 0.0, 1.0, 1.0),  # tx, ty, sx, sy
        }
        return handle

    def _h_canvas_set_transform(self, ctx: int, tx: float, ty: float, sx: float, sy: float, rot: float) -> int:
        canvas = self._canvases.get(ctx)
        if canvas:
            if rot:
                log.debug("canvas: rotation not supported (ignored)")
            canvas["transform"] = (tx, ty, sx, sy)
        return 0

    @staticmethod
    def _apply_transform(canvas: dict[str, Any], x: float, y: float, w: float, h: float):
        tx, ty, sx, sy = canvas["transform"]
        return tx + x * sx, ty + y * sy, w * sx, h * sy

    def _h_canvas_draw_image(self, ctx: int, img: int, x: float, y: float, w: float, h: float) -> int:
        canvas = self._canvases.get(ctx)
        image = self._images.get(img)
        if not canvas or image is None:
            return -1
        px, py, pw, ph = self._apply_transform(canvas, x, y, w, h)
        if pw <= 0 or ph <= 0:
            return 0
        resized = image.resize((max(1, int(pw)), max(1, int(ph))))
        canvas["image"].paste(resized, (int(px), int(py)))
        return 0

    def _h_canvas_copy_image(
        self, ctx: int, img: int,
        sx_: float, sy_: float, sw: float, sh: float,
        dx: float, dy: float, dw: float, dh: float,
    ) -> int:
        """Crop: copies the (sx,sy,sw,sh) rect of the image to (dx,dy,dw,dh)."""
        canvas = self._canvases.get(ctx)
        image = self._images.get(img)
        if not canvas or image is None:
            return -1
        src = image.crop((int(sx_), int(sy_), int(sx_ + sw), int(sy_ + sh)))
        tx, ty, tsx, tsy = canvas["transform"]
        dx2, dy2, dw2, dh2 = tx + dx * tsx, ty + dy * tsy, dw * tsx, dh * tsy
        if dw2 <= 0 or dh2 <= 0:
            return 0
        if (int(dw2), int(dh2)) != src.size:
            src = src.resize((max(1, int(dw2)), max(1, int(dh2))))
        canvas["image"].paste(src, (int(dx2), int(dy2)))
        return 0

    def _h_canvas_noop_ok(self, *args) -> int:
        return 0

    def _h_canvas_new_font(self, *args) -> int:
        return 1  # fixed handle; draw_text is a no-op

    def _h_canvas_new_image(self, ptr: int, length: int) -> int:
        from io import BytesIO
        from PIL import Image as PILImage

        data = self._mem_read(ptr, length)
        try:
            image = PILImage.open(BytesIO(data))
            image.load()
        except Exception as e:  # noqa: BLE001
            log.warning("canvas.new_image: decode falhou: %s", e)
            return -3
        handle = self._img_next
        self._img_next += 1
        self._images[handle] = image.convert("RGBA")
        return handle

    def _h_canvas_get_image(self, ctx: int) -> int:
        if ctx not in self._canvases:
            return -1
        handle = self._img_next
        self._img_next += 1
        self._images[handle] = self._canvases[ctx]["image"].copy()
        return handle

    def _h_canvas_get_image_data(self, img: int) -> int:
        """Retorna handle std com os bytes PNG da imagem."""
        from io import BytesIO

        image = self._images.get(img)
        if image is None:
            return -2
        buf = BytesIO()
        image.save(buf, format="PNG")
        return self._store_value(buf.getvalue(), str_encode=False)

    def _h_canvas_get_image_width(self, img: int) -> float:
        image = self._images.get(img)
        return float(image.width) if image is not None else -2.0

    def _h_canvas_get_image_height(self, img: int) -> float:
        image = self._images.get(img)
        return float(image.height) if image is not None else -2.0

    # ------------------------------------------------------------------ #
    # public API (host -> source exports)
    # ------------------------------------------------------------------ #

    def get_search_manga_list(self, query: str, page: int, filters: list | None = None) -> models.MangaPageResult:
        # NOTE: like rakuyomi, the query is stored WITHOUT str-encoding —
        # sources read it as raw utf8 bytes (a postcard varint prefix would
        # leak into the string as a control char, e.g. 0x0D = '\r').
        hq = self._store_value(query, str_encode=False)
        hf = self._store_value(filters or [], str_encode=False)
        try:
            data = self._call_ptr("get_search_manga_list", (hq, page, hf))
        finally:
            self._std_destroy(hq)
            self._std_destroy(hf)
        return models.decode_manga_page_result(data)

    def get_manga_list(self, listing=None) -> models.MangaPageResult:
        hf = self._store_value([], str_encode=False)
        try:
            data = self._call_ptr("get_manga_list", (1, hf))
        finally:
            self._std_destroy(hf)
        return models.decode_manga_page_result(data)

    def get_manga_update(self, manga: models.Manga, needs_details: bool = True, needs_chapters: bool = True) -> models.Manga:
        handle = self._store_value(manga, str_encode=False)
        try:
            data = self._call_ptr(
                "get_manga_update",
                (handle, 1 if needs_details else 0, 1 if needs_chapters else 0),
            )
        finally:
            self._std_destroy(handle)
        return models.decode_manga(data)

    def get_page_list(self, manga: models.Manga, chapter: models.Chapter) -> list[models.Page]:
        hm = self._store_value(manga, str_encode=False)
        hc = self._store_value(chapter, str_encode=False)
        try:
            data = self._call_ptr("get_page_list", (hm, hc))
        finally:
            self._std_destroy(hm)
            self._std_destroy(hc)
        return models.decode_page_list(data)

    def fetch_image(self, url: str, headers: dict | None = None) -> bytes:
        resp = request_with_limit(self._http, "GET", url, headers=headers or {})
        resp.raise_for_status()
        return resp.content
