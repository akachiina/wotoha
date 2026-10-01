"""Gerenciamento do Suwayomi-Server (download do jar, start/stop/status)."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import time
from pathlib import Path

import requests

from .client import SuwayomiClient
from .config import config
from .errors import JavaNotFound, ServerJarNotFound

# Release estável mais recente; o nome do asset .jar é versionado, então
# descobrimos via API do GitHub.
GITHUB_LATEST_API = "https://api.github.com/repos/Suwayomi/Suwayomi-Server/releases/latest"

JAVA_CANDIDATES = ("java", "/usr/bin/java", "/usr/local/bin/java")


def find_java() -> str:
    """Encontra o binário java."""
    for candidate in JAVA_CANDIDATES:
        try:
            subprocess.run(
                [candidate, "-version"],
                capture_output=True,
                check=True,
            )
            return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    raise JavaNotFound(
        "Java não encontrado. Instale com: sudo apt install default-jre (ou equivalente)"
    )


def _latest_jar_url() -> str:
    resp = requests.get(GITHUB_LATEST_API, timeout=30)
    resp.raise_for_status()
    for asset in resp.json().get("assets", []):
        name: str = asset.get("name", "")
        if name.endswith(".jar"):
            return asset["browser_download_url"]
    raise ServerJarNotFound("Nenhum asset .jar encontrado no release mais recente do Suwayomi")


def download_jar(dest: Path | None = None, force: bool = False) -> Path:
    """Baixa o jar do Suwayomi-Server mais recente (com barra de progresso)."""
    dest = dest or config.server_jar
    if dest.is_file() and not force:
        print(f"Jar já existe: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = _latest_jar_url()
    print(f"Baixando {url.split('/')[-1]} para {dest} ...")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r  {pct:3d}%  ({done / 1048576:.1f}/{total / 1048576:.1f} MB)", end="", flush=True)
    print()
    return dest


def start(background: bool = True) -> subprocess.Popen:
    """Inicia o Suwayomi-Server."""
    jar = config.server_jar
    if not jar.is_file():
        raise ServerJarNotFound(
            f"Jar não encontrado em {jar}. Execute primeiro: wotoha server download"
        )
    java = find_java()

    config.server_log.parent.mkdir(parents=True, exist_ok=True)

    if SuwayomiClient().is_up():
        print("Servidor já está rodando.")
        return None  # type: ignore[return-value]

    log_file = open(config.server_log, "ab")  # noqa: SIM115 - vive pelo processo
    proc = subprocess.Popen(
        [java, "-jar", str(jar)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    config.server_pid.write_text(str(proc.pid))
    print(f"Servidor iniciado (pid {proc.pid}). Log: {config.server_log}")

    if background:
        # aguarda subir
        client = SuwayomiClient()
        for _ in range(90):  # até 90s
            if client.is_up():
                print(f"Servidor pronto em {config.server_url}")
                return proc
            if proc.poll() is not None:
                raise ServerJarNotFound(
                    f"Processo morreu com código {proc.returncode}. Veja o log: {config.server_log}"
                )
            time.sleep(1)
        print("Aviso: servidor não respondeu em 90s; pode ainda estar iniciando.")
    return proc


def stop() -> None:
    """Para o servidor (via pid file)."""
    if not config.server_pid.is_file():
        print("Nenhum pid file; servidor não foi iniciado pelo wotoha.")
        return
    pid = int(config.server_pid.read_text().strip())
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"SIGTERM enviado para {pid}")
    except ProcessLookupError:
        print(f"Processo {pid} não existe mais.")
    config.server_pid.unlink(missing_ok=True)


def status() -> bool:
    """Mostra o status do servidor. Retorna True se estiver no ar."""
    client = SuwayomiClient()
    if client.is_up():
        print(f"ONLINE em {config.server_url}")
        return True
    print(f"OFFLINE ({config.server_url})")
    return False


def ensure_running() -> None:
    """Garante que o servidor está no ar, iniciando se necessário."""
    if SuwayomiClient().is_up():
        return
    print("Servidor não está no ar; iniciando...")
    start(background=True)


atexit.register(lambda: None)  # placeholder; stop é manual
