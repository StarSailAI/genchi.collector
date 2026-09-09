from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import service
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from egress import BlockedDestination, EgressProxy, PublicResolver, public_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "ftp://example.com/",
        "https://example.com:5432/",
        "http://example.com:bad/",
    ],
)
def test_browser_rejects_unsupported_urls(url):
    with pytest.raises(BlockedDestination):
        public_url(url)


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["100.64.0.1"], ["8.8.8.8", "192.168.1.1"]],
)
def test_resolver_rejects_private_and_mixed_dns_answers(addresses):
    async def run():
        resolver = PublicResolver()
        asyncio.get_running_loop().getaddrinfo = AsyncMock(
            return_value=[(None, None, None, None, (ip, 0)) for ip in addresses]
        )
        with pytest.raises(BlockedDestination):
            await resolver.resolve("example.test")

    asyncio.run(run())


def test_vpn_fake_dns_is_resolved_over_https_instead_of_allowed(monkeypatch):
    async def run():
        resolver = PublicResolver()
        asyncio.get_running_loop().getaddrinfo = AsyncMock(
            return_value=[(None, None, None, None, ("198.18.0.2", 0))]
        )
        fallback = AsyncMock(return_value={"8.8.8.8"})
        monkeypatch.setattr(resolver, "resolve_doh", fallback)
        assert await resolver.resolve("example.test") == "8.8.8.8"
        fallback.assert_awaited_once_with("example.test")
        # A resolver returning a private answer must still be rejected.
        resolver.cache.clear()
        fallback.return_value = {"10.0.0.1"}
        with pytest.raises(BlockedDestination):
            await resolver.resolve("example.test")

    asyncio.run(run())


def test_proxy_connect_pins_validated_ip_and_relays_then_closes(monkeypatch):
    async def run():
        async def echo(reader, writer):
            try:
                writer.write(await reader.read(4))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        real_connect = asyncio.open_connection
        calls = []

        async def connect(host, port_number):
            calls.append((host, port_number))
            return await real_connect("127.0.0.1", port)

        proxy = EgressProxy(SimpleNamespace(resolve=AsyncMock(return_value="8.8.8.8")))
        await proxy.start()
        monkeypatch.setattr(asyncio, "open_connection", connect)
        try:
            reader, writer = await real_connect(
                "127.0.0.1", proxy.server.sockets[0].getsockname()[1]
            )
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            await writer.drain()
            assert b"200 Connection Established" in await reader.readuntil(b"\r\n\r\n")
            writer.write(b"test")
            await writer.drain()
            assert await reader.readexactly(4) == b"test"
            assert calls == [("8.8.8.8", 443)]
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
            upstream.close()
            await upstream.wait_closed()
        assert not proxy.tasks

    asyncio.run(run())


def test_proxy_blocks_private_connect_and_disallowed_ports():
    async def run():
        proxy = EgressProxy(PublicResolver())
        await proxy.start()
        try:
            for target in (b"127.0.0.1:443", b"example.com:5432"):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy.server.sockets[0].getsockname()[1]
                )
                writer.write(b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
                await writer.drain()
                assert b"403 Forbidden" in await reader.read()
                writer.close()
                await writer.wait_closed()
        finally:
            await proxy.close()

    asyncio.run(run())


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.closed = False

    def on(self, *_):
        pass

    async def route_web_socket(self, *_):
        pass

    async def route(self, *_):
        pass

    async def goto(self, url, **_):
        self.url = url
        return SimpleNamespace(status=200)

    async def wait_for_selector(self, *_args, **_kwargs):
        pass

    async def wait_for_timeout(self, *_):
        pass

    async def content(self):
        return "<html><h1>日本の活動</h1></html>"

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.closed = False

    async def new_page(self):
        return FakePage(self)

    async def close(self):
        self.closed = True


class FakeBrowser:
    version = "152.0.4"

    def __init__(self):
        self.contexts = []

    def is_connected(self):
        return True

    async def new_context(self, **options):
        assert options == {
            "accept_downloads": False,
            "service_workers": "block",
            "ignore_https_errors": False,
        }
        context = FakeContext()
        self.contexts.append(context)
        return context


def test_browser_auth_render_contract_and_public_session_isolation(monkeypatch):
    async def run():
        monkeypatch.setenv("BROWSER_API_TOKEN", "test-browser-token")
        app = service.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["browser"] = FakeBrowser()
        app["x_context"] = FakeContext()

        async def validate(host):
            if host == "127.0.0.1":
                raise BlockedDestination("Private")
            return "8.8.8.8"

        monkeypatch.setattr(service.RESOLVER, "resolve", validate)
        async with TestClient(TestServer(app)) as client:
            health = await (await client.get("/health")).json()
            assert health["engine"] == "camoufox" and health["route"] == "F"
            assert (await client.post("/fetch", json={"url": "https://example.com"})).status == 401
            headers = {"Authorization": "Bearer test-browser-token"}
            assert (
                await client.post("/fetch", headers=headers, json={"url": "http://127.0.0.1/"})
            ).status == 400
            response = await client.post(
                "/fetch",
                headers=headers,
                json={"url": "https://example.com/news/", "selector": "h1"},
            )
            assert response.status == 200
            assert "日本の活動" in await response.text()
            assert response.headers["X-Genchi-Browser-Upstream-Status"] == "200"
            assert response.headers["X-Genchi-Browser-Final-URL"] == "https://example.com/news/"
        assert len(app["browser"].contexts) == 1 and app["browser"].contexts[0].closed
        assert not app["x_context"].closed

        async def missing_selector(*_args, **_kwargs):
            raise TimeoutError("requested news list never appeared")

        monkeypatch.setattr(FakePage, "wait_for_selector", missing_selector)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/fetch", headers=headers,
                json={"url": "https://example.com/news/", "selector": ".missing-news-list"},
            )
            assert response.status == 502
            assert app["browser"].contexts[-1].closed

        async def rate_limited(page, url, **_):
            page.url = url
            return SimpleNamespace(status=429, headers={"retry-after": "90"})

        monkeypatch.setattr(FakePage, "goto", rate_limited)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/fetch", headers=headers,
                json={"url": "https://example.com/tickets", "selector": ".not-on-rate-limit-page"},
            )
            assert response.status == 200
            assert response.headers["X-Genchi-Browser-Upstream-Status"] == "429"
            assert response.headers["Retry-After"] == "90"
            assert app["browser"].contexts[-1].closed

    asyncio.run(run())


def test_browser_guard_blocks_private_subresources(monkeypatch):
    async def run():
        async def denied(_):
            raise web.HTTPBadRequest(text=json.dumps({"code": "PRIVATE_ADDRESS"}))

        monkeypatch.setattr(service, "_validate_public_url", denied)
        route = SimpleNamespace(abort=AsyncMock())
        assert not await service._guard_public(
            route, SimpleNamespace(url="http://127.0.0.1/private")
        )
        route.abort.assert_awaited_once_with("blockedbyclient")

    asyncio.run(run())


def test_browser_child_exit_recovers_before_next_public_page(monkeypatch):
    async def run():
        app = service.create_app()
        app["browser"] = SimpleNamespace(is_connected=lambda: False)
        app["browser_manager"] = object()
        app["x_context"] = FakeContext()
        calls = []

        async def stop(current):
            calls.append("stop")
            current["browser"] = None

        async def start(current):
            calls.append("start")
            current["browser"] = FakeBrowser()

        monkeypatch.setattr(service, "stop_browser", stop)
        monkeypatch.setattr(service, "start_browser", start)
        request = SimpleNamespace(app=app)
        page = await service._new_page(request)
        assert calls == ["stop", "start"]
        assert app["browser"].is_connected()
        await service._close_page(request, page)
        assert page.context.closed

    asyncio.run(run())
