"""Private HTTP/CONNECT proxy: resolve public addresses, then connect to that IP.

Adapted from SkillHub's website-access architecture. No browser credential or
model key enters this proxy; HTTPS stays encrypted between Firefox and the site.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit

import aiohttp

FAKE_DNS = ipaddress.ip_network("198.18.0.0/15")


class BlockedDestination(ValueError):
    pass


def public_url(url: str):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
            or len(url) > 8192
        ):
            raise ValueError
        return parsed
    except ValueError:
        raise BlockedDestination(
            "Only public HTTP(S) addresses on ports 80/443 are allowed"
        ) from None


class PublicResolver:
    def __init__(self):
        self.cache = {}

    async def resolve(self, hostname: str) -> str:
        host = hostname.rstrip(".").lower()
        if (
            not re.fullmatch(r"[a-z0-9.-]+", host)
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise BlockedDestination("Invalid public hostname")
        cached = self.cache.get(host)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        except OSError:
            raise BlockedDestination("DNS unavailable") from None
        addresses = {record[4][0] for record in records}
        if addresses and all(ipaddress.ip_address(address) in FAKE_DNS for address in addresses):
            # Local VPN fake-IP answers are never allowed through. Resolve real A
            # records over verified HTTPS; an ordinary private answer stays blocked.
            addresses = await self.resolve_doh(host)
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise BlockedDestination("Private or reserved address")
        address = sorted(addresses)[0]
        if len(self.cache) >= 512:
            self.cache.clear()
        self.cache[host] = (address, time.monotonic() + 60)
        return address

    async def resolve_doh(self, hostname: str) -> set[str]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(
                "https://1.1.1.1/dns-query",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise BlockedDestination("DNS unavailable")
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise BlockedDestination("Invalid DNS response")
                import json

                data = json.loads(raw)
                if data.get("Status") != 0:
                    raise BlockedDestination("DNS unavailable")
                return {
                    record["data"] for record in data.get("Answer", []) if record.get("type") == 1
                }


class EgressProxy:
    def __init__(self, resolver: PublicResolver):
        self.resolver = resolver
        self.server = None
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=32768)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def handle(self, reader, writer):
        if len(self.tasks) >= 64:
            writer.close()
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        started = False
        try:
            async with asyncio.timeout(180):
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                lines = header.decode("latin1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise BlockedDestination("Invalid proxy request")
                if method == "CONNECT":
                    if not re.fullmatch(r"[A-Za-z0-9.-]+:443", target):
                        raise BlockedDestination("Invalid HTTPS target")
                    host, port = target.rsplit(":", 1)
                    outbound = None
                else:
                    parsed = public_url(target)
                    if parsed.scheme != "http" or method not in {"GET", "HEAD", "OPTIONS"}:
                        raise BlockedDestination("Invalid HTTP request")
                    host, port = parsed.hostname, parsed.port or 80
                    # Force one HTTP request per connection. CONNECT is an opaque TLS
                    # tunnel; Firefox still validates the actual website certificate.
                    kept = []
                    for line in lines[1:]:
                        if not line:
                            continue
                        name, value = line.split(":", 1)
                        if name.lower() in {
                            "host",
                            "connection",
                            "proxy-connection",
                            "proxy-authorization",
                            "cookie",
                            "authorization",
                            "content-length",
                            "transfer-encoding",
                        }:
                            continue
                        kept.append(f"{name}:{value}")
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    outbound = (
                        f"{method} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nConnection: close\r\n"
                        + "\r\n".join(kept)
                        + "\r\n\r\n"
                    ).encode("latin1")
                ip = await asyncio.wait_for(self.resolver.resolve(host), 10)
                # Use the validated IP directly: no second DNS lookup / rebinding.
                incoming, upstream = await asyncio.wait_for(
                    asyncio.open_connection(ip, int(port)), 10
                )
                if outbound is None:
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    upstream.write(outbound)
                    await upstream.drain()
                started = True

                async def pipe(source, destination):
                    size = 0
                    while chunk := await asyncio.wait_for(source.read(65536), 30):
                        size += len(chunk)
                        if size > 32_000_000:
                            raise BlockedDestination("Connection byte limit")
                        destination.write(chunk)
                        await destination.drain()

                if outbound is None:
                    relays = [
                        asyncio.create_task(pipe(reader, upstream)),
                        asyncio.create_task(pipe(incoming, writer)),
                    ]
                    try:
                        await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for relay in relays:
                            relay.cancel()
                        await asyncio.gather(*relays, return_exceptions=True)
                else:
                    await pipe(incoming, writer)
        except (
            ValueError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            aiohttp.ClientError,
        ):
            if not started:
                with contextlib.suppress(OSError):
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
        finally:
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 2)
            self.tasks.discard(task)
