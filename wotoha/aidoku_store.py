"""Instalador de sources Aidoku (.aix) para o wotoha."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import requests

from .config import config

log = logging.getLogger(__name__)

DEFAULT_INDEX_URL = "https://aidoku-community.github.io/sources/index.min.json"


def sources_dir() -> Path:
    d = Path(config.sources_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_index(index_url: str = DEFAULT_INDEX_URL) -> list[dict[str, Any]]:
    resp = requests.get(index_url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("sources", [])
    return data


def _base_url_of(index_url: str) -> str:
    # index at https://x/y/index.min.json -> package base https://x/y/
    return index_url.rsplit("/", 1)[0] + "/"


def install(source_id: str, index_url: str = DEFAULT_INDEX_URL) -> Path:
    """Baixa o .aix do source e instala em ~/.wotoha/sources/<id>.aix."""
    sources = fetch_index(index_url)
    entry = next((s for s in sources if s.get("id") == source_id), None)
    if entry is None:
        raise KeyError(f"source '{source_id}' não encontrado no índice")

    url = entry["downloadURL"]
    if not url.startswith("http"):
        url = _base_url_of(index_url) + url

    dest = sources_dir() / f"{source_id}.aix"
    log.info("baixando %s -> %s", url, dest)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    record_source_repo(source_id, index_url)
    return dest


def uninstall(source_id: str) -> bool:
    dest = sources_dir() / f"{source_id}.aix"
    if dest.exists():
        dest.unlink()
        _forget_source_repo(source_id)
        return True
    return False


# proveniência: de qual índice cada source instalado veio

def _source_repos_file() -> Path:
    return Path(config.sources_dir).expanduser().parent / "source_repos.json"


def record_source_repo(source_id: str, index_url: str) -> None:
    f = _source_repos_file()
    data: dict[str, str] = {}
    if f.is_file():
        try:
            data = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            data = {}
    data[source_id] = index_url
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _forget_source_repo(source_id: str) -> None:
    f = _source_repos_file()
    if not f.is_file():
        return
    try:
        data = json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return
    if source_id in data:
        del data[source_id]
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def repo_of(source_id: str) -> str | None:
    """Nome curto do repo de origem (ou URL se não tiver nome)."""
    f = _source_repos_file()
    if not f.is_file():
        return None
    try:
        url = json.loads(f.read_text()).get(source_id)
    except Exception:  # noqa: BLE001
        return None
    if not url:
        return None
    for name, u in list_repos().items():
        if u == url:
            return name
    return url


def list_installed() -> list[dict[str, Any]]:
    """Metadados de todos os sources instalados (sem bootar WASM)."""
    result = []
    for aix in sorted(sources_dir().glob("*.aix")):
        try:
            import zipfile

            with zipfile.ZipFile(aix) as zf:
                info = json.loads(zf.read("Payload/source.json")).get("info", {})
            result.append(
                {
                    "id": info.get("id", aix.stem),
                    "name": info.get("name", aix.stem),
                    "languages": info.get("languages"),
                    "version": info.get("version"),
                    "url": info.get("url"),
                    "file": str(aix),
                }
            )
        except Exception as e:  # noqa: BLE001
            log.warning("source inválido %s: %s", aix.name, e)
    return result


def search_index(term: str, index_url: str = DEFAULT_INDEX_URL) -> list[dict[str, Any]]:
    term = term.lower()
    return [
        s
        for s in fetch_index(index_url)
        if term in s.get("name", "").lower() or term in s.get("id", "").lower()
    ]


# --------------------------------------------------------------------------- #
# repositórios nomeados (~/.wotoha/repos.json)
# --------------------------------------------------------------------------- #

BUILTIN_REPOS = {"aidoku": DEFAULT_INDEX_URL}


def _repos_file() -> Path:
    d = Path(config.sources_dir).expanduser().parent
    d.mkdir(parents=True, exist_ok=True)
    return d / "repos.json"


def list_repos() -> dict[str, str]:
    repos = dict(BUILTIN_REPOS)
    f = _repos_file()
    if f.is_file():
        try:
            repos.update(json.loads(f.read_text()))
        except Exception as e:  # noqa: BLE001
            log.warning("repos.json inválido: %s", e)
    return repos


def add_repo(name: str, url: str) -> None:
    f = _repos_file()
    current: dict[str, str] = {}
    if f.is_file():
        try:
            current = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            current = {}
    current[name] = url
    f.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")


def remove_repo(name: str) -> bool:
    f = _repos_file()
    current: dict[str, str] = {}
    if f.is_file():
        try:
            current = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            current = {}
    if name not in current:
        return False
    del current[name]
    f.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    return True


def resolve_index(value: str | None) -> str:
    """Aceita URL explícita ou nome de repo cadastrado (ex.: 'yomu')."""
    if not value:
        return DEFAULT_INDEX_URL
    if value.startswith("http"):
        return value
    repos = list_repos()
    if value in repos:
        return repos[value]
    known = ", ".join(sorted(repos))
    raise KeyError(
        f"repo '{value}' não cadastrado. Use: wotoha sources repo add {value} <url> "
        f"(cadastrados: {known})"
    )
