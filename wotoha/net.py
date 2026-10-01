"""Camada HTTP compartilhada: proxy, timeout e rate limiting.

Portado dos conceitos de rede do mangadex-downloader
(REF/mangadex-downloader/mangadex_downloader/network.py).
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import requests

from .config import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Limita a N chamadas por período de `per` segundos.

    Sem dependências externas (token bucket simplificado).
    """

    def __init__(self, calls: int, per: float) -> None:
        self.calls = max(1, int(calls))
        self.per = max(0.0, float(per))
        self._lock = threading.Lock()
        self._timestamps: list[float] = []

    def wait(self) -> None:
        if self.per <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < self.per]
                if len(self._timestamps) < self.calls:
                    self._timestamps.append(now)
                    return
                wait_for = self.per - (now - self._timestamps[0])
            if wait_for > 0:
                time.sleep(min(wait_for, 0.5))


_limiter_lock = threading.Lock()
_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter | None:
    global _limiter
    with _limiter_lock:
        if _limiter is None and (config.rate_limit_calls > 0):
            _limiter = RateLimiter(config.rate_limit_calls, config.rate_limit_seconds)
        return _limiter


def reset_rate_limiter() -> None:
    global _limiter
    with _limiter_lock:
        _limiter = None


def apply_network_options(
    session: requests.Session,
    proxy: str | None = None,
    timeout: float | None = None,
) -> requests.Session:
    """Aplica proxy/timeout de config em uma Session (in place)."""
    proxy = proxy or config.proxy
    if proxy:
        scheme = urlparse(proxy).scheme or "http"
        if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            raise ValueError(
                f"proxy inválido: '{proxy}' (use http(s):// ou socks5://; "
                "socks requer pip install requests[socks])"
            )
        session.proxies = {"http": proxy, "https": proxy}
        log.info("proxy: %s", proxy)
    if timeout is not None:
        session._wotoha_timeout = timeout  # type: ignore[attr-defined]
    return session


def request_with_limit(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float | None = None,
    **kwargs,
) -> requests.Response:
    """request() com rate limiting respeitado (default 2.5s entre requests,
    igual ao default do mangadex-downloader)."""
    limiter = get_rate_limiter()
    if limiter:
        limiter.wait()
    effective_timeout = timeout or getattr(session, "_wotoha_timeout", None) or 30.0
    return session.request(method, url, timeout=effective_timeout, **kwargs)
