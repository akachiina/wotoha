"""Suwayomi-Server management (jar download, start/stop/status)."""

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

# Latest stable release; the .jar asset name is versioned, so we look it up
# via the GitHub API.
GITHUB_LATEST_API = "https://api.github.com/repos/Suwayomi/Suwayomi-Server/releases/latest"

JAVA_CANDIDATES = ("java", "/usr/bin/java", "/usr/local/bin/java")


def find_java() -> str:
    """Finds the java binary."""
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
        "Java not found. Install it with: sudo apt install default-jre (or equivalent)"
    )


def _latest_jar_url() -> str:
    resp = requests.get(GITHUB_LATEST_API, timeout=30)
    resp.raise_for_status()
    for asset in resp.json().get("assets", []):
        name: str = asset.get("name", "")
        if name.endswith(".jar"):
            return asset["browser_download_url"]
    raise ServerJarNotFound("No .jar asset found in the latest Suwayomi release")


def download_jar(dest: Path | None = None, force: bool = False) -> Path:
    """Downloads the latest Suwayomi-Server jar (with a progress bar)."""
    dest = dest or config.server_jar
    if dest.is_file() and not force:
        print(f"Jar already exists: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = _latest_jar_url()
    print(f"Downloading {url.split('/')[-1]} to {dest} ...")
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
    """Starts the Suwayomi-Server."""
    jar = config.server_jar
    if not jar.is_file():
        raise ServerJarNotFound(
            f"Jar not found at {jar}. Run first: wotoha server download"
        )
    java = find_java()

    config.server_log.parent.mkdir(parents=True, exist_ok=True)

    if SuwayomiClient().is_up():
        print("Server is already running.")
        return None  # type: ignore[return-value]

    log_file = open(config.server_log, "ab")  # noqa: SIM115 - lives for the process lifetime
    proc = subprocess.Popen(
        [java, "-jar", str(jar)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    config.server_pid.write_text(str(proc.pid))
    print(f"Server started (pid {proc.pid}). Log: {config.server_log}")

    if background:
        # wait for it to come up
        client = SuwayomiClient()
        for _ in range(90):  # up to 90s
            if client.is_up():
                print(f"Server ready at {config.server_url}")
                return proc
            if proc.poll() is not None:
                raise ServerJarNotFound(
                    f"Process died with code {proc.returncode}. See the log: {config.server_log}"
                )
            time.sleep(1)
        print("Warning: server did not respond within 90s; it may still be starting.")
    return proc


def stop() -> None:
    """Stops the server (via pid file)."""
    if not config.server_pid.is_file():
        print("No pid file; the server was not started by wotoha.")
        return
    pid = int(config.server_pid.read_text().strip())
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"SIGTERM sent to {pid}")
    except ProcessLookupError:
        print(f"Process {pid} no longer exists.")
    config.server_pid.unlink(missing_ok=True)


def status() -> bool:
    """Shows the server status. Returns True when it is up."""
    client = SuwayomiClient()
    if client.is_up():
        print(f"ONLINE at {config.server_url}")
        return True
    print(f"OFFLINE ({config.server_url})")
    return False


def ensure_running() -> None:
    """Ensures the server is up, starting it if needed."""
    if SuwayomiClient().is_up():
        return
    print("Server is not up; starting...")
    start(background=True)


atexit.register(lambda: None)  # placeholder; stop is manual
