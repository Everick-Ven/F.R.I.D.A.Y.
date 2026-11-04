"""Безопасный HTTP-клиент для обхода публичных сайтов (защита от SSRF и DNS rebinding)."""

import asyncio
import ipaddress
import socket
from time import monotonic

import httpx


class UnsafeURLError(httpx.RequestError):
    """Запрошенный адрес выходит за пределы публичного интернета."""


_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _public_address(value: str) -> str:
    if "%" in value:
        raise ValueError("Scoped IPv6 addresses are not allowed")
    address = ipaddress.ip_address(value)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Address is not public")
    if isinstance(address, ipaddress.IPv6Address):
        if (
            address.is_site_local
            or address.ipv4_mapped
            or address.sixtofour
            or address.teredo
            or any(address in net for net in _TRANSLATION_NETWORKS)
        ):
            raise ValueError("Translated IPv6 addresses are not allowed")
    return str(address)


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.raw_host.decode("ascii")
        if (
            url.scheme not in {"http", "https"}
            or not host
            or url.username
            or url.password
            or "%" in host
            or host.rstrip(".").lower() == "localhost"
            or host.rstrip(".").lower().endswith(".localhost")
        ):
            raise UnsafeURLError("Разрешены только публичные HTTP(S) сайты", request=request)
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise UnsafeURLError("Некорректный порт", request=request)

        timeout = request.extensions.get("timeout", {}).get("connect")
        connect_budget = timeout if timeout is not None else 10.0
        deadline = monotonic() + connect_budget

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        try:
            if literal is not None:
                addresses = [_public_address(host)]
            else:
                answers = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                    timeout=connect_budget,
                )
                addresses = list(dict.fromkeys(_public_address(row[4][0]) for row in answers))
                if not addresses:
                    raise ValueError("DNS не вернул адресов")
                addresses.sort(key=lambda val: ipaddress.ip_address(val).version)
        except ValueError as exc:
            raise UnsafeURLError("Домен указывает на непубличный IP-адрес", request=request) from exc
        except asyncio.TimeoutError as exc:
            raise httpx.ConnectTimeout("Таймаут DNS-разрешения", request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError("Ошибка DNS-разрешения", request=request) from exc

        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        for idx, address in enumerate(addresses):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise httpx.ConnectTimeout("Исчерпан лимит времени подключения", request=request)
            extensions = {
                **request.extensions,
                "sni_hostname": host,
                "timeout": {**request.extensions.get("timeout", {}), "connect": remaining},
            }
            pinned = httpx.Request(
                method=request.method,
                url=url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await self._transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if idx == len(addresses) - 1:
                    raise

    async def aclose(self) -> None:
        await self._transport.aclose()


def public_http_client(
    *,
    timeout: float | httpx.Timeout = 15.0,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Создать защищённый от SSRF HTTP-клиент для чтения внешних сайтов."""
    return httpx.AsyncClient(
        transport=PublicHTTPTransport(),
        trust_env=False,
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        max_redirects=8,
    )
