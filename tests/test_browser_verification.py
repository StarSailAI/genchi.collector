from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import service
from aiohttp.test_utils import TestClient, TestServer
from verification import VerificationPointer, supported_url


class Panel:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def filter(self, **_):
        return self

    async def count(self):
        return int(self.page.challenged) if self.selector == '#captcha-container' else int(not self.page.challenged)

    async def bounding_box(self):
        return {'x': 20, 'y': 20, 'width': 400, 'height': 300}

    async def inner_text(self):
        return 'Solve the puzzle' if self.page.challenged else 'News article details ' * 30


class Page:
    viewport_size = None

    def __init__(self):
        self.challenged, self.closed, self.navigations = True, False, 0
        self.url = 'https://natalie.mu/comic'
        self.context = SimpleNamespace(close=AsyncMock(side_effect=self.close))
        self.mouse = SimpleNamespace(click=AsyncMock(), move=AsyncMock(), down=AsyncMock(), up=AsyncMock())

    def on(self, *_):
        pass

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def route(self, *_):
        pass

    async def set_viewport_size(self, size):
        self.viewport_size = size

    async def goto(self, url, **_):
        self.navigations += 1
        self.url = url
        return SimpleNamespace(status=405 if self.challenged else 200)

    async def title(self):
        return 'Human Verification' if self.challenged else 'Comic news'

    def locator(self, selector):
        return Panel(self, selector)

    async def screenshot(self, **_):
        return b'fake-image'

    async def wait_for_timeout(self, *_):
        pass

    async def wait_for_selector(self, *_, **__):
        pass

    async def content(self):
        return '<html>News article details</html>'


def test_original_navigation_control_budget_and_resume(monkeypatch):
    async def run():
        monkeypatch.setenv('BROWSER_API_TOKEN', 'test')
        monkeypatch.setattr(service, '_validate_public_url', AsyncMock())
        page = Page()
        monkeypatch.setattr(service, '_new_page', AsyncMock(return_value=page))
        app = service.create_app()
        app.on_startup.remove(service.start_browser)
        # Preserve cleanup_ctx's cleanup handler; remove only real browser stop.
        app.on_cleanup.remove(service.stop_browser)
        headers = {'Authorization': 'Bearer test'}
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/verification')).status == 401
            first = await (await client.post('/fetch', headers=headers, json={'url': page.url})).json()
            rid = first['verification']['id']
            again = await (await client.post('/fetch', headers=headers, json={'url': page.url})).json()
            assert again['verification']['id'] == rid and page.navigations == 1
            different = await (await client.post('/fetch', headers=headers, json={'url': 'https://natalie.mu/music'})).json()
            assert different['code'] == 'VERIFICATION_IN_PROGRESS' and page.navigations == 1
            assert app['browser_semaphore'].locked()
            path = '/verification/'+rid
            assert (await client.post(path+'/observe', headers=headers, json={})).status == 401
            claim = await (await client.post(path+'/claim', headers=headers, json={})).json()
            control = {**headers, 'X-Verification-Control': claim['controlToken']}
            assert (await client.post(path+'/claim', headers=headers, json={})).status == 409
            obs = await (await client.post(path+'/observe', headers=control, json={})).json()
            command = {'requestId': 'one-action-request-id', 'input': {'observationId': obs['observationId'], 'kind': 'click', 'x': 60, 'y': 70}}
            result = await (await client.post(path+'/pointer', headers=control, json=command)).json()
            assert result['performed']
            await client.post(path+'/pointer', headers=control, json=command)
            assert page.mouse.click.await_count == 1
            assert (await client.post(path+'/resume', headers=control, json={})).status == 409
            assert page.navigations == 1
            page.challenged = False
            app['verification'].run['status'] = 200
            assert (await client.post(path+'/resume', headers=control, json={})).status == 200
            assert (await client.get(path+'/result', headers=headers)).status == 200
            assert page.navigations == 1 and not page.closed and not app['browser_semaphore'].locked()
            assert (await client.post(path+'/observe', headers=control, json={})).status == 401
            await client.post('/fetch', headers=headers, json={'url': 'https://natalie.mu/music'})
            assert page.navigations == 2  # Same page/context, no cookie export.
            late = await client.get(path+'/result', headers=headers)
            assert late.headers['X-Genchi-Browser-Final-URL'] == 'https://natalie.mu/comic'
            page.challenged = True
            next_challenge = await (await client.post('/fetch', headers=headers, json={'url': 'https://natalie.mu/comic'})).json()
            next_id = next_challenge['verification']['id']
            assert next_id != rid and next_challenge['verification']['pointerActions'] == 1
            path = '/verification/'+next_id
            claim = await (await client.post(path+'/claim', headers=headers, json={})).json()
            control = {**headers, 'X-Verification-Control': claim['controlToken']}
            for i in range(11):
                obs = await (await client.post(path+'/observe', headers=control, json={})).json()
                result = await (await client.post(path+'/pointer', headers=control, json={
                    'requestId': f'bounded-command-{i:03d}', 'input': {'observationId': obs['observationId'], 'kind': 'click', 'x': 60, 'y': 70}})).json()
                assert result['performed']
            assert (await client.post(path+'/pointer', headers=control, json={'requestId': 'thirteenth-command-denied', 'input': {}})).status == 409
            assert page.mouse.click.await_count == 12
        assert page.closed

    asyncio.run(run())


@pytest.mark.parametrize('bad', [
    {'kind': 'click', 'x': 0, 'y': 0},
    {'kind': 'click', 'x': float('nan'), 'y': 40},
    {'kind': 'click', 'x': 40, 'y': 40, 'script': 'anything'},
    {'kind': 'drag', 'x': 40, 'y': 40, 'toX': 80, 'toY': 70, 'durationMs': 9999},
])
def test_pointer_rejects_outside_invalid_and_consumed_observation(bad):
    async def run():
        page = Page()
        await page.set_viewport_size({'width': 800, 'height': 600})
        pointer = VerificationPointer(page)
        obs = await pointer.observe()
        assert not (await pointer.perform({'observationId': obs['observationId'], **bad}))['performed']
        assert (await pointer.perform({'observationId': obs['observationId'], 'kind': 'click', 'x': 40, 'y': 40}))['reason'] == 'observe_required'
        assert page.mouse.click.await_count == 0
    asyncio.run(run())


def test_expired_control_closes_context_and_releases_slot(monkeypatch):
    async def run():
        page = Page()
        app = service.create_app()
        manager = app['verification']
        await app['browser_semaphore'].acquire()
        manager.run = {'id': 'run', 'page': page, 'pointer': VerificationPointer(page), 'state': 'WAITING',
                       'lease': 'secret', 'lease_until': 0, 'created': 0, 'waiting_since': 0, 'owns_slot': True}
        await manager.expire()
        assert manager.run['state'] == 'EXPIRED' and not manager.run['lease']
        assert page.closed and not app['browser_semaphore'].locked()
    asyncio.run(run())


def test_registered_site_scope():
    assert supported_url('https://natalie.mu/comic/news/123')
    for url in ('https://natalie.mu.evil.test/comic', 'https://user@natalie.mu/comic', 'http://natalie.mu/comic', 'https://natalie.mu:8443/music', 'https://natalie.mu/comical'):
        assert not supported_url(url)


def test_drag_releases_mouse_on_driver_failure_and_rejects_stale_image():
    async def run():
        page = Page()
        await page.set_viewport_size({'width': 800, 'height': 600})
        pointer = VerificationPointer(page)
        obs = await pointer.observe()
        page.url += '/news/123'
        assert (await pointer.perform({'observationId': obs['observationId'], 'kind': 'click', 'x': 40, 'y': 40}))['reason'] == 'observation_changed'
        obs = await pointer.observe()
        page.mouse.move.side_effect = [None, RuntimeError('driver stopped')]
        with pytest.raises(RuntimeError):
            await pointer.perform({'observationId': obs['observationId'], 'kind': 'drag', 'x': 40, 'y': 40, 'toX': 100, 'toY': 40, 'durationMs': 1000})
        page.mouse.up.assert_awaited_once()
    asyncio.run(run())


def test_worker_waits_for_original_result_without_replaying_navigation(monkeypatch):
    import genchi_fetchers.fetchers as fetchers
    from genchi_fetchers.fetchers import BrowserClient
    rid = 'verification-run-123'
    pending = SimpleNamespace(status_code=409, json=lambda: {'code': 'VERIFICATION_REQUIRED', 'verification': {'id': rid, 'state': 'WAITING'}})
    ready = SimpleNamespace(status_code=200, text='<article>recovered original page</article>', headers={'X-Genchi-Browser-Upstream-Status': '200', 'X-Genchi-Browser-Final-URL': 'https://natalie.mu/comic'})
    calls, replies = [], iter([pending, fetchers.requests.Timeout('lost result'), ready])
    monkeypatch.setattr(fetchers.requests, 'post', lambda *a, **k: (calls.append(a[0]), pending)[1])
    def get(*a, **k):
        response = next(replies)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(fetchers.requests, 'get', get)
    monkeypatch.setattr(fetchers.time, 'sleep', lambda *_: None)
    assert 'recovered original' in BrowserClient('http://browser:3003', 'test').render('https://natalie.mu/comic')[0]
    assert calls == ['http://browser:3003/fetch']


def test_refreshed_puzzle_in_same_rectangle_rejects_old_screenshot():
    async def run():
        page = Page()
        await page.set_viewport_size({'width': 800, 'height': 600})
        pointer = VerificationPointer(page)
        obs = await pointer.observe()
        page.screenshot = AsyncMock(return_value=b'new-puzzle-same-rectangle')
        result = await pointer.perform({'observationId': obs['observationId'], 'kind': 'click', 'x': 40, 'y': 40})
        assert result == {'performed': False, 'reason': 'observation_changed'}
        page.mouse.click.assert_not_awaited()
        fresh = await pointer.observe()
        assert (await pointer.perform({'observationId': fresh['observationId'], 'kind': 'click', 'x': 40, 'y': 40}))['performed']
    asyncio.run(run())


def test_drag_attempts_have_separate_three_round_budget(monkeypatch):
    async def run():
        monkeypatch.setenv('BROWSER_API_TOKEN', 'test')
        monkeypatch.setattr(service, '_validate_public_url', AsyncMock())
        page = Page()
        monkeypatch.setattr(service, '_new_page', AsyncMock(return_value=page))
        app = service.create_app()
        app.on_startup.remove(service.start_browser)
        app.on_cleanup.remove(service.stop_browser)
        headers = {'Authorization': 'Bearer test'}
        async with TestClient(TestServer(app)) as client:
            first = await (await client.post('/fetch', headers=headers, json={'url': page.url})).json()
            path = '/verification/'+first['verification']['id']
            claim = await (await client.post(path+'/claim', headers=headers, json={})).json()
            control = {**headers, 'X-Verification-Control': claim['controlToken']}
            for i in range(4):
                obs = await (await client.post(path+'/observe', headers=control, json={})).json()
                reply = await (await client.post(path+'/pointer', headers=control, json={
                    'requestId': f'bounded-drag-round-{i}', 'input': {'observationId': obs['observationId'],
                    'kind': 'drag', 'x': 40, 'y': 40, 'toX': 100, 'toY': 40, 'durationMs': 100}})).json()
                if i < 3:
                    assert reply['performed']
                else:
                    assert reply['code'] == 'DRAG_BUDGET_EXHAUSTED'
            assert page.mouse.up.await_count == 3
    asyncio.run(run())


def test_cropped_observation_binds_same_clip_and_keeps_global_pointer_coordinates():
    async def run():
        page = Page()
        await page.set_viewport_size({'width': 800, 'height': 600})
        page.screenshot = AsyncMock(return_value=b'panel-image')
        pointer = VerificationPointer(page)
        obs = await pointer.observe(panel_only=True)
        assert obs['imageOrigin'] == {'x': 20, 'y': 20}
        assert page.screenshot.call_args.kwargs['clip'] == obs['actionScope']
        assert (await pointer.perform({'observationId': obs['observationId'], 'kind': 'click', 'x': 40, 'y': 40}))['performed']
        assert page.screenshot.call_args_list[0].kwargs == page.screenshot.call_args_list[1].kwargs
        page.mouse.click.assert_awaited_once_with(40, 40)
    asyncio.run(run())


def test_registered_grid_geometry_uses_only_visible_elements():
    async def run():
        page = Page()
        await page.set_viewport_size({'width': 800, 'height': 600})
        class Elements:
            def __init__(self, values): self.values = values
            def filter(self, **kwargs): return self
            async def count(self): return len(self.values)
            def nth(self, i): return self.values[i]
        class Element:
            def __init__(self, box, text=''): self.box, self.text = box, text
            async def bounding_box(self): return self.box
            async def inner_text(self): return self.text
        grid = Element({'x': 30, 'y': 30, 'width': 270, 'height': 270})
        button = Element({'x': 310, 'y': 260, 'width': 60, 'height': 30}, 'Confirm')
        original = page.locator
        def locator(selector):
            panel = original(selector)
            panel.locator = lambda child: Elements([grid] if 'canvas' in child else [button])
            return panel
        page.locator = locator
        controls = await VerificationPointer(page).image_controls()
        assert len(controls['imageTiles']) == 9
        assert controls['imageTiles'][4] == {'x': 120, 'y': 120, 'width': 90, 'height': 90}
        assert controls['registeredControls']['confirm'] == button.box
    asyncio.run(run())


def test_browser_loss_after_resolved_request_can_recover_without_replaying_that_result(monkeypatch):
    async def run():
        monkeypatch.setenv('BROWSER_API_TOKEN', 'test')
        monkeypatch.setattr(service, '_validate_public_url', AsyncMock())
        first, replacement = Page(), Page()
        first.challenged = replacement.challenged = False
        monkeypatch.setattr(service, '_new_page', AsyncMock(side_effect=[first, replacement]))
        app = service.create_app()
        app.on_startup.remove(service.start_browser)
        app.on_cleanup.remove(service.stop_browser)
        headers = {'Authorization': 'Bearer test'}
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/fetch', headers=headers, json={'url': first.url})).status == 200
            manager = app['verification']
            rid = manager.run['id']
            manager.results[rid] = ('original recovered HTML', first.url, 200, 10**12)
            first.closed = True
            assert (await client.post('/fetch', headers=headers, json={'url': 'https://natalie.mu/music'})).status == 200
            assert replacement.navigations == 1
            old = await client.get('/verification/'+rid+'/result', headers=headers)
            assert await old.text() == 'original recovered HTML'
    asyncio.run(run())


@pytest.mark.parametrize('terminal', [False, True])
def test_idle_expiry_can_reopen_but_failed_intervention_requires_cooldown(monkeypatch, terminal):
    async def run():
        import time

        from verification import FAILED_COOLDOWN_SECONDS, SESSION_SECONDS
        monkeypatch.setenv('BROWSER_API_TOKEN', 'test')
        monkeypatch.setattr(service, '_validate_public_url', AsyncMock())
        first, replacement = Page(), Page()
        first.challenged = replacement.challenged = False
        new_page = AsyncMock(side_effect=[first, replacement])
        monkeypatch.setattr(service, '_new_page', new_page)
        app = service.create_app()
        app.on_startup.remove(service.start_browser)
        app.on_cleanup.remove(service.stop_browser)
        headers = {'Authorization': 'Bearer test'}
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/fetch', headers=headers, json={'url': first.url})).status == 200
            manager = app['verification']
            if terminal:
                await manager.close('CANCELED', 'operator_canceled')
                assert (await client.post('/fetch', headers=headers, json={'url': first.url})).status == 409
                assert new_page.await_count == 1
                manager.run['closed_at'] = time.monotonic()-FAILED_COOLDOWN_SECONDS-1
            else:
                manager.run['created'] = time.monotonic()-SESSION_SECONDS-1
            assert (await client.post('/fetch', headers=headers, json={'url': first.url})).status == 200
            assert new_page.await_count == 2
            assert first.closed and replacement.navigations == 1
    asyncio.run(run())
