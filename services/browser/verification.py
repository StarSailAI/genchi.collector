"""Private, bounded intervention in the original Natalie browser session.

Adapted from SkillHub's interruption and VerificationPointer contracts. No
selectors, scripts, cookies or navigation targets are accepted as pointer input.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

PANEL = "#captcha-container"
NEWS_LINKS = 'a[href*="/comic/news/"], a[href*="/music/news/"]'
# Natalie's observed 3x3 image challenge can require nine selections, Start and
# Confirm. The six-action slider recipe cannot complete that ordinary form.
# Keep a separate fixed bound for this registered image-selection workflow.
MAX_POINTERS = 12
LEASE_SECONDS = 120
WAIT_SECONDS = 600
SESSION_SECONDS = 1800
FAILED_COOLDOWN_SECONDS = 6 * 3600


def supported_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "natalie.mu" and not p.username
                and not p.password and p.port in (None, 443)
                and any(p.path == root or p.path.startswith(root + "/") for root in ("/comic", "/music")))
    except ValueError:
        return False


def fail(code, status=409):
    return web.json_response({"code": code}, status=status, headers={"Cache-Control": "no-store"})


class VerificationPointer:
    def __init__(self, page):
        self.page, self.observation, self.picture = page, None, None

    async def scope(self):
        panels = self.page.locator(PANEL).filter(visible=True)
        if await panels.count() != 1:
            return None
        box, viewport = await panels.bounding_box(), self.page.viewport_size
        if not box or not viewport:
            return None
        x, y = max(0, box['x']), max(0, box['y'])
        right, bottom = min(viewport['width'], box['x'] + box['width']), min(viewport['height'], box['y'] + box['height'])
        return {'x': x, 'y': y, 'width': right-x, 'height': bottom-y} if right > x and bottom > y else None

    async def observe(self, *, panel_only=False):
        self.observation = None
        box = await self.scope()
        # Bind the command to the actual returned screenshot, including the
        # puzzle contents: a new puzzle can occupy exactly the same rectangle.
        options = {'type': 'jpeg', 'quality': 80, 'scale': 'css', 'timeout': 5000}
        if panel_only and box:
            options['clip'] = box
        self.picture = await self.page.screenshot(**options)
        if not box:
            return {}
        oid = secrets.token_urlsafe(18)
        digest = hashlib.sha256(self.picture).digest()
        self.observation = (oid, time.monotonic(), self.page.url, box, digest, options)
        return {'observationId': oid, 'actionScope': box, 'viewport': self.page.viewport_size,
                **({'imageOrigin': {'x': box['x'], 'y': box['y']}} if panel_only else {})}

    async def image_controls(self):
        """Geometry of public, rendered controls; no scripts or hidden answers."""
        panel = self.page.locator(PANEL).filter(visible=True)
        if await panel.count() != 1:
            return {}
        scope = await self.scope()
        def inside(box):
            return box and scope and box['x'] >= scope['x'] and box['y'] >= scope['y'] and (
                box['x']+box['width'] <= scope['x']+scope['width']+1 and
                box['y']+box['height'] <= scope['y']+scope['height']+1)
        boxes = []
        elements = panel.locator('canvas,img,svg,[style*="background-image"]').filter(visible=True)
        for i in range(min(await elements.count(), 40)):
            box = await elements.nth(i).bounding_box()
            if inside(box) and min(box['width'], box['height']) >= 75 and abs(box['width']-box['height']) < 3 and box not in boxes:
                boxes.append(box)
        tiles = []
        if len(boxes) == 9:
            tiles = sorted(boxes, key=lambda b: (round((b['y']+b['height']/2)/30), b['x']+b['width']/2))
        elif len(boxes) == 1 and boxes[0]['width'] >= 240:
            grid = boxes[0]
            tiles = [{'x': grid['x']+col*grid['width']/3, 'y': grid['y']+row*grid['height']/3,
                      'width': grid['width']/3, 'height': grid['height']/3} for row in range(3) for col in range(3)]
        controls = {}
        buttons = panel.locator('button,[role="button"],input[type="button"],input[type="submit"]').filter(visible=True)
        for i in range(min(await buttons.count(), 20)):
            button = buttons.nth(i)
            label = ''.join((await button.inner_text() or await button.get_attribute('value') or '').split())
            name = {'開始': 'start', '確認': 'confirm', 'Start': 'start', 'Confirm': 'confirm'}.get(label)
            box = await button.bounding_box()
            if name and inside(box):
                controls[name] = box
        return {'imageTiles': tiles, 'registeredControls': controls}

    async def perform(self, data):
        old, self.observation = self.observation, None
        if not isinstance(data, dict) or not old or data.get('observationId') != old[0]:
            return {'performed': False, 'reason': 'observe_required'}
        if time.monotonic()-old[1] > 60 or self.page.url != old[2] or await self.scope() != old[3]:
            return {'performed': False, 'reason': 'observation_changed'}
        kind = data.get('kind')
        keys = {'observationId', 'kind', 'x', 'y'} | ({'toX', 'toY', 'durationMs'} if kind == 'drag' else set())
        if kind not in {'click', 'drag'} or set(data) != keys:
            return {'performed': False, 'reason': 'invalid_pointer'}
        if any(type(data[k]) not in (int, float) or not math.isfinite(data[k]) for k in keys-{'observationId', 'kind'}):
            return {'performed': False, 'reason': 'invalid_pointer'}
        box = old[3]

        def inside(x, y):
            return box['x'] <= x < box['x']+box['width'] and box['y'] <= y < box['y']+box['height']

        if not inside(data['x'], data['y']):
            return {'performed': False, 'reason': 'outside_panel'}
        if kind == 'drag' and (not inside(data['toX'], data['toY']) or not 100 <= data['durationMs'] <= 3000):
            return {'performed': False, 'reason': 'invalid_drag'}
        picture = await self.page.screenshot(**old[5])
        if hashlib.sha256(picture).digest() != old[4]:
            return {'performed': False, 'reason': 'observation_changed'}
        mouse = self.page.mouse
        if kind == 'click':
            await mouse.click(data['x'], data['y'])
        else:
            await mouse.move(data['x'], data['y'])
            await mouse.down()
            try:
                for step in range(1, 31):
                    ratio = step / 30
                    await mouse.move(data['x']+(data['toX']-data['x'])*ratio, data['y']+(data['toY']-data['y'])*ratio)
                    await self.page.wait_for_timeout(data['durationMs']/30)
                # Commit the final movement using normal mouse events, as in
                # SkillHub's tested endpoint fix; never read a hidden answer.
                sx, sy = data['toX'], data['toY']
                if abs(sx-data['x']) >= abs(sy-data['y']):
                    sy += 1 if inside(sx, sy+1) else -1
                else:
                    sx += 1 if inside(sx+1, sy) else -1
                if inside(sx, sy):
                    await mouse.move(sx, sy)
                    await self.page.wait_for_timeout(50)
                await mouse.move(data['toX'], data['toY'])
                await self.page.wait_for_timeout(50)
            finally:
                await mouse.up()
        await self.page.wait_for_timeout(500)
        return {'performed': True}


class VerificationManager:
    def __init__(self, app, *, authorized, new_page, close_page, guard_public):
        self.app, self.authorized = app, authorized
        self.new_page, self.close_page, self.guard_public = new_page, close_page, guard_public
        self.run, self.lock = None, asyncio.Lock()
        self.results = {}

    def public(self):
        r = self.run
        return {k: r[k] for k in ('id', 'url', 'state', 'pointerActions', 'interruptions', 'reason')} if r else None

    async def close(self, state, reason):
        r = self.run
        if r:
            r.update(state=state, reason=reason, lease=None)
            r.setdefault('closed_at', time.monotonic())
            r['pointer'].observation = None
            try:
                if not r['page'].is_closed():
                    await r['page'].context.close()
            finally:
                if r['owns_slot']:
                    self.app['browser_semaphore'].release()
                    r['owns_slot'] = False

    async def expire(self):
        r = self.run
        if not r or r['state'] in {'EXPIRED', 'FAILED', 'CANCELED'}:
            return
        now = time.monotonic()
        if r['page'].is_closed():
            await self.close('FAILED', 'browser_closed')
            return
        if r['state'] == 'READY' and now > r['created']+SESSION_SECONDS:
            await self.close('EXPIRED', 'idle_session_expired')
            return
        if (now > r['created']+SESSION_SECONDS
                or (r['state'] == 'WAITING' and now > r['waiting_since']+WAIT_SECONDS)
                or (r['lease'] and now > r['lease_until'])):
            await self.close('EXPIRED', 'session_or_control_timeout')

    async def challenge(self, page):
        return await page.locator(PANEL).filter(visible=True).count() > 0 or 'human verification' in (await page.title()).lower()

    async def ready(self, page):
        if page.is_closed() or not supported_url(page.url) or await self.challenge(page):
            return False
        return await page.locator(NEWS_LINKS).count() > 0 and len(await page.locator('body').inner_text()) > 200

    async def html(self):
        r = self.run
        html = await r['page'].content()
        if len(html.encode()) > 12_582_912:
            await self.close('FAILED', 'page_too_large')
            return fail('PAGE_TOO_LARGE', 502)
        return web.Response(text=html, content_type='text/html', headers={
            'X-Genchi-Browser-Upstream-Status': str(r['status']),
            'X-Genchi-Browser-Final-URL': r['page'].url, 'Cache-Control': 'no-store'})

    async def fetch(self, request, url, *, selector='', timeout=90, wait=3):
        """A repeated pending fetch returns the same run, without replaying navigation."""
        async with self.lock:
            await self.expire()
            if (self.run and self.run['state'] in {'FAILED', 'CANCELED', 'EXPIRED'} and
                    (self.run['reason'] == 'idle_session_expired' or
                     time.monotonic()-self.run.get('closed_at', time.monotonic()) >= FAILED_COOLDOWN_SECONDS)):
                # A later daily task may start its own session after the fixed
                # failure cooldown. Never immediately replay the failed run.
                self.run = None
            if (self.run and self.run['id'] in self.results and self.run['page'].is_closed()
                    and self.run['reason'] in {'browser_closed', 'browser_navigation_failed'}):
                # The original challenge already resolved. A later read-only
                # navigation lost its browser, so _new_page may recover it.
                # Unresolved/canceled/exhausted interventions never take this path.
                self.run = None
            if self.run and self.run['state'] not in {'READY'}:
                if url != self.run['url']:
                    return fail('VERIFICATION_IN_PROGRESS')
                return web.json_response({'code': 'VERIFICATION_REQUIRED', 'verification': self.public()}, status=409)
            if self.run and self.run['page'].is_closed():
                await self.close('FAILED', 'browser_closed')
                return fail('BROWSER_CLOSED')
            try:
                await asyncio.wait_for(self.app['browser_semaphore'].acquire(), 5)
            except TimeoutError:
                return fail('BROWSER_BUSY', 503)
            if not self.run:
                page = None
                try:
                    page = await self.new_page(request)
                    # Camoufox's default no_viewport mode yields viewport_size=None.
                    # Establish CSS coordinates before navigation or observation.
                    await page.set_viewport_size({'width': 1280, 'height': 900})
                except BaseException:
                    if page is not None and not page.is_closed():
                        await page.context.close()
                    self.app['browser_semaphore'].release()
                    raise
                self.run = {'id': secrets.token_urlsafe(18), 'url': url, 'page': page,
                            'pointer': VerificationPointer(page), 'state': 'LOADING', 'reason': None,
                            'pointerActions': 0, 'interruptions': 0, 'created': time.monotonic(),
                            'waiting_since': None, 'lease': None, 'lease_until': 0,
                            'owns_slot': True, 'status': 0, 'commands': {}}

                def document_response(response):
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        self.run['status'] = response.status

                page.on('response', document_response)

                async def guard(route, nav):
                    if nav.is_navigation_request() and nav.frame == page.main_frame and not supported_url(nav.url):
                        await route.abort('blockedbyclient')
                    elif await self.guard_public(route, nav):
                        # Natalie news text is server-rendered. Once a normal
                        # document arrives, do not execute ads or load media.
                        # The 405 verification document retains its required
                        # scripts, images and API calls in this same context.
                        normal = 200 <= self.run['status'] < 300
                        auxiliary = nav.resource_type in {'image', 'font', 'media', 'stylesheet', 'script', 'xhr', 'fetch', 'other'}
                        subframe = nav.is_navigation_request() and nav.frame != page.main_frame
                        if normal and (auxiliary or subframe):
                            await route.abort('blockedbyclient')
                        else:
                            await route.continue_()

                try:
                    await page.route('**/*', guard)
                except BaseException:
                    await self.close('FAILED', 'browser_setup_failed')
                    raise
            r = self.run
            r['owns_slot'], r['url'] = True, url
            try:
                response = await r['page'].goto(url, wait_until='domcontentloaded', timeout=int(timeout*1000))
                r['status'] = response.status if response else 0
                await r['page'].wait_for_timeout(int(wait*1000))
                if await self.challenge(r['page']):
                    if r['id'] in self.results:
                        # New interruption, same context and cumulative budgets.
                        # Late readers of the prior request keep its own result.
                        r['id'], r['commands'] = secrets.token_urlsafe(18), {}
                    r['interruptions'] += 1
                    if r['interruptions'] > 3 or r['pointerActions'] >= MAX_POINTERS:
                        await self.close('FAILED', 'verification_budget_exhausted')
                    else:
                        r.update(state='WAITING', waiting_since=time.monotonic(), reason='visible_captcha')
                    return web.json_response({'code': 'VERIFICATION_REQUIRED', 'verification': self.public()}, status=409)
                if selector and r['status'] < 400:
                    await r['page'].wait_for_selector(selector, state='attached', timeout=15_000)
                if r['status'] >= 400:
                    r['state'] = 'READY'
                    return await self.html()  # Preserve real 404/410/429 semantics.
                if not await self.ready(r['page']):
                    await self.close('FAILED', 'upstream_error_or_content_not_ready')
                    return fail('CONTENT_NOT_READY', 502)
                r['state'] = 'READY'
                return await self.html()
            except BaseException:
                await self.close('FAILED', 'browser_navigation_failed')
                raise
            finally:
                if r['state'] != 'WAITING' and r['owns_slot']:
                    self.app['browser_semaphore'].release()
                    r['owns_slot'] = False

    async def handle(self, request):
        if not self.authorized(request):
            return fail('UNAUTHORIZED', 401)
        async with self.lock:
            await self.expire()
            action = request.match_info.get('action', 'list')
            r = self.run
            if action == 'list':
                return web.json_response({'items': [self.public()] if r else []})
            if not r or request.match_info['id'] != r['id']:
                return fail('UNKNOWN_VERIFICATION', 404)
            data = await request.json()
            if action == 'claim':
                if r['state'] != 'WAITING' or r['lease']:
                    return fail('CONTROL_UNAVAILABLE')
                token = secrets.token_urlsafe(32)
                r.update(lease=token, lease_until=time.monotonic()+LEASE_SECONDS)
                return web.json_response({'controlToken': token, 'leaseSeconds': LEASE_SECONDS, **self.public()})
            token = request.headers.get('X-Verification-Control', '')
            if not r['lease'] or not secrets.compare_digest(token, r['lease']):
                return fail('CONTROL_REQUIRED', 401)
            if action == 'cancel':
                await self.close('CANCELED', 'operator_canceled')
                return web.json_response(self.public())
            if action == 'observe':
                p = r['page']
                if p.is_closed():
                    await self.close('FAILED', 'browser_closed')
                    return fail('BROWSER_CLOSED')
                capability = await r['pointer'].observe(panel_only=data.get('imageMode') == 'panel')
                picture = r['pointer'].picture
                controls = await r['pointer'].image_controls() if data.get('imageMode') == 'panel' and capability else {}
                return web.json_response({**self.public(), **capability, **controls, 'pageUrl': p.url,
                    'title': await p.title(), 'text': (await p.locator('body').inner_text())[:2500],
                    'actions': ['pointer'] if capability and r['pointerActions'] < r.get('pointerLimit', MAX_POINTERS) else [],
                    'image': base64.b64encode(picture).decode(), 'mimeType': 'image/jpeg'})
            if action == 'pointer':
                key = data.get('requestId')
                if not isinstance(key, str) or not 16 <= len(key) <= 80:
                    return fail('INVALID_REQUEST_ID', 400)
                if key in r['commands']:
                    old, reply = r['commands'][key]
                    return web.json_response(reply) if old == data else fail('REQUEST_ID_REUSED')
                pointer_input = data.get('input')
                if not isinstance(pointer_input, dict):
                    return fail('INVALID_POINTER', 400)
                limit = 6 if pointer_input.get('kind') == 'drag' else r.get('pointerLimit', MAX_POINTERS)
                r['pointerLimit'] = limit
                if pointer_input.get('kind') == 'drag' and r.get('dragActions', 0) >= 3:
                    return fail('DRAG_BUDGET_EXHAUSTED')
                if r['pointerActions'] >= limit:
                    return fail('POINTER_BUDGET_EXHAUSTED')
                # Every attempted pointer command consumes budget, including a
                # failed/incomplete driver action. Lost replies cannot replay it.
                r['pointerActions'] += 1
                if pointer_input.get('kind') == 'drag':
                    r['dragActions'] = r.get('dragActions', 0) + 1
                reply = {'performed': False, 'reason': 'action_interrupted'}
                r['commands'][key] = (data, reply)
                reply = await r['pointer'].perform(data.get('input'))
                r['commands'][key] = (data, reply)
                return web.json_response({**reply, 'pointerActions': r['pointerActions']})
            if action == 'resume':
                if not 200 <= r['status'] < 300 or not await self.ready(r['page']):
                    return fail('VERIFICATION_NOT_RESOLVED')
                # Read the recovered original navigation; do not submit it again.
                r.update(state='READY', lease=None, reason=None)
                r['pointer'].observation = None
                if r['owns_slot']:
                    self.app['browser_semaphore'].release()
                    r['owns_slot'] = False
                response = await self.html()
                if response.status == 200:
                    self.results[r['id']] = (response.text, r['page'].url, r['status'], time.monotonic())
                return response
            return fail('UNKNOWN_ACTION', 400)

    async def result(self, request):
        if not self.authorized(request):
            return fail('UNAUTHORIZED', 401)
        async with self.lock:
            await self.expire()
            r = self.run
            completed = self.results.get(request.match_info['id'])
            if completed:
                body, url, status, _created = completed
                return web.Response(text=body, content_type='text/html', headers={
                    'X-Genchi-Browser-Upstream-Status': str(status),
                    'X-Genchi-Browser-Final-URL': url, 'Cache-Control': 'no-store'})
            if not r or request.match_info['id'] != r['id']:
                return fail('UNKNOWN_VERIFICATION', 404)
            if r['state'] != 'READY' or not await self.ready(r['page']):
                return web.json_response({'code': 'VERIFICATION_REQUIRED', 'verification': self.public()}, status=409)
            return await self.html()

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(5)
            async with self.lock:
                await self.expire()
                self.results = {key: value for key, value in self.results.items() if time.monotonic()-value[3] <= SESSION_SECONDS}
                # Keep terminal tombstones briefly; callers cannot reset a run's
                # action budget by reopening the same URL immediately.
                if self.run and self.run['state'] in {'EXPIRED', 'FAILED', 'CANCELED'} and time.monotonic() > self.run['created']+SESSION_SECONDS:
                    self.run = None
