"""SSRF 対策（LAN・ループバック宛ての拒否）と Cloudflare Access の JWT 検証。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import urlsplit

import jwt


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """インターネット上の宛先として扱ってよいアドレスか。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class HostGuard:
    """PC 側で取得してよい URL かを判定する。

    名前解決した結果がすべてグローバルアドレスの場合だけ許可する。PC の Chrome は
    外部サイトの JS も実行するため、ページの取得だけでなくサブリソースにも適用する。
    DNS リバインディングまでは防げないため、Access による利用者の限定が前提となる。
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        block_domains: Iterable[str] = (),
        ttl: float = 300.0,
    ) -> None:
        self._allow_private = allow_private
        self._block_domains = tuple(d.lower().strip(".") for d in block_domains if d)
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host:
            return False
        if self.is_blocked_domain(host):
            return False
        if self._allow_private:
            return True

        now = time.monotonic()
        cached = self._cache.get(host)
        if cached is not None and cached[0] > now:
            return cached[1]
        ok = await self._resolves_to_public(host)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[host] = (now + self._ttl, ok)
        return ok

    def is_blocked_domain(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self._block_domains)

    @staticmethod
    async def _resolves_to_public(host: str) -> bool:
        try:
            return is_public_ip(ipaddress.ip_address(host))
        except ValueError:
            pass
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        addresses = {info[4][0].split("%")[0] for info in infos}
        return bool(addresses) and all(is_public_ip(ipaddress.ip_address(a)) for a in addresses)


class _SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class AccessVerifier:
    """Cloudflare Access が付与する Cf-Access-Jwt-Assertion ヘッダーを検証する。"""

    HEADER = "cf-access-jwt-assertion"

    def __init__(
        self,
        team_domain: str,
        aud: str,
        allowed_emails: Iterable[str] = (),
        *,
        jwks_client: _SigningKeySource | None = None,
    ) -> None:
        self._issuer = f"https://{team_domain.strip('/')}"
        self._aud = aud
        self._allowed = {e.lower() for e in allowed_emails}
        self._jwks = jwks_client or jwt.PyJWKClient(
            f"{self._issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
        )

    def verify(self, token: str) -> dict[str, Any]:
        """検証に失敗した場合は jwt.PyJWTError を送出する（同期処理。鍵の取得で通信する）。"""
        key = self._jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=self._aud, issuer=self._issuer
        )
        if self._allowed and str(claims.get("email", "")).lower() not in self._allowed:
            raise jwt.InvalidTokenError("許可されていないメールアドレスです")
        return claims
