from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image, ImageDraw
from verification_agent import Agent, AgentError, Ledger, pointer_input


def observation(index=0):
    image = Image.new('RGB', (320, 528), 'white')
    draw = ImageDraw.Draw(image)
    for i in range(min(index, 2)):
        draw.rectangle((i*100+10, 100, i*100+85, 175), fill='orange')
    output = BytesIO()
    image.save(output, format='JPEG', quality=80)
    return {'pageUrl': 'https://natalie.mu/comic', 'text': 'Select the requested images',
            'imageOrigin': {'x': 480, 'y': 8}, 'actionScope': {'x': 480, 'y': 8, 'width': 320, 'height': 528},
            'image': base64.b64encode(output.getvalue()).decode(),
            'observationId': f'observation-{index}', 'actions': ['pointer'],
            'imageTiles': [{'x': 480+c*100, 'y': 100+r*100, 'width': 100, 'height': 100} for r in range(3) for c in range(3)],
            'registeredControls': {'confirm': {'x': 730, 'y': 470, 'width': 60, 'height': 30}}}


def test_ledger_reserves_paid_calls_before_response_and_survives_restart(tmp_path):
    path = tmp_path/'state.json'
    ledger = Ledger(path)
    assert ledger.begin('run', 'vision')
    ledger.reserve('run', 2, 2)
    ledger.reserve('run', 2, 2)
    with pytest.raises(AgentError, match='budget'):
        ledger.reserve('run', 2, 2)
    restarted = Ledger(path)
    assert restarted.data['runs']['run']['status'] == 'abandoned'
    assert not restarted.begin('run', 'vision')
    restarted.begin('next', 'vision')
    with pytest.raises(AgentError, match='budget'):
        restarted.reserve('next', 2, 2)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('decision', [
    {'matching_tiles': [0], 'selected_tiles': []},
    {'matching_tiles': [10], 'selected_tiles': []},
    {'matching_tiles': [True], 'selected_tiles': []},
    {'matching_tiles': [1, 1], 'selected_tiles': []},
    {'matching_tiles': [1], 'selected_tiles': [2]},
    {'matching_tiles': [1], 'selected_tiles': [], 'script': 'anything'},
    {'action': 'navigate', 'url': 'https://other.test'},
])
def test_untrusted_model_cannot_extend_operation_scope(decision):
    with pytest.raises(AgentError):
        pointer_input(decision, observation())


def test_classification_maps_to_actual_browser_geometry_once():
    result, _ = pointer_input({'matching_tiles': [1, 2], 'selected_tiles': [1]}, observation())
    assert result == {'observationId': 'observation-0', 'kind': 'click', 'x': 630, 'y': 150}
    confirm, _ = pointer_input({'matching_tiles': [1, 2], 'selected_tiles': [1, 2]}, observation())
    assert confirm['x'] == 760 and confirm['y'] == 485


def test_autonomous_operator_checks_resume_and_preserves_single_observation(tmp_path):
    async def run():
        ledger = Ledger(tmp_path/'state.json')
        actions, model_requests = [], []

        async def model(request):
            value = await request.json()
            assert ledger.data['runs']['run']['modelCalls'] == len(model_requests)+1
            model_requests.append(value)
            return web.json_response({'choices': [{'finish_reason': 'stop', 'message': {'content':
                json.dumps({'category': 'bag', 'objects': ['bag']*9, 'matching_tiles': [1, 2]})}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})

        app = web.Application()
        app.router.add_post('/model', model)
        async with TestServer(app) as server:
            import aiohttp
            agent = Agent(ledger, base='http://browser:3003', token='test', model_url=str(server.make_url('/model')),
                          model_key='test-key', model='vision')
            async def bridge(action, rid=None, token=None, data=None):
                if action == 'claim':
                    return 200, {'controlToken': 'private', 'leaseSeconds': 120}
                if action == 'resume':
                    return (200, {'ready': True}) if len(actions) == 3 else (409, {})
                if action == 'observe':
                    assert data == {'imageMode': 'panel'}
                    return 200, observation(len(actions))
                assert action == 'pointer'
                assert data['input']['observationId'] == f'observation-{len(actions)}'
                actions.append(data)
                return 200, {'performed': True, 'pointerActions': len(actions)}
            agent.bridge = bridge
            async with aiohttp.ClientSession() as agent.session:
                await agent.handle({'id': 'run', 'url': 'https://natalie.mu/comic'})
        assert len(model_requests) == 1
        assert all(m['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,') for m in model_requests)
        assert ledger.data['runs']['run']['status'] == 'resolved'
        assert ledger.data['runs']['run']['promptTokens'] == 100
        assert (tmp_path/'initial-puzzle.jpg').read_bytes() == base64.b64decode(observation()['image'])
        assert (tmp_path/'initial-puzzle.jpg').stat().st_mode & 0o777 == 0o600
        assert len({a['requestId'] for a in actions}) == 3
    asyncio.run(run())


def test_operator_cancels_after_ambiguous_model_failure_and_does_not_retry(tmp_path):
    async def run():
        ledger = Ledger(tmp_path/'state.json')
        agent = Agent(ledger, base='http://browser:3003', token='test', model_url='https://model.test', model_key='secret', model='vision')
        agent.bridge = AsyncMock(side_effect=[(200, {'controlToken': 'private', 'leaseSeconds': 120}), (409, {}),
                                              (200, observation()), (200, {})])
        agent.decide = AsyncMock(side_effect=TimeoutError('paid reply lost'))
        await agent.handle({'id': 'run', 'url': 'https://natalie.mu/comic'})
        assert ledger.data['runs']['run']['status'] == 'failed'
        assert agent.bridge.call_args_list[-1].args[0] == 'cancel'
        assert agent.decide.await_count == 1
        await agent.handle({'id': 'run', 'url': 'https://natalie.mu/comic'})
        assert agent.decide.await_count == 1
    asyncio.run(run())


def test_selection_receipt_rejects_new_puzzle_and_unchanged_image():
    from verification_agent import verify_selection_progress
    before, after = observation(0), observation(1)
    tile = before['imageTiles'][0]
    origin = before['imageOrigin']
    verify_selection_progress(before['image'], after['image'], tile, origin)
    with pytest.raises(AgentError, match='selection_not_visible'):
        verify_selection_progress(before['image'], before['image'], tile, origin)
    with pytest.raises(AgentError, match='challenge_changed'):
        verify_selection_progress(before['image'], observation(2)['image'], tile, origin)


def test_operator_never_reuses_plan_after_puzzle_or_layout_changes(tmp_path):
    async def run():
        ledger = Ledger(tmp_path/'state.json')
        agent = Agent(ledger, base='http://browser:3003', token='test', model_url='https://model.test', model_key='secret', model='vision')
        agent.decide = AsyncMock(return_value={'matching_tiles': [1, 2], 'selected_tiles': []})
        agent.bridge = AsyncMock(side_effect=[
            (200, {'controlToken': 'private', 'leaseSeconds': 120}), (409, {}), (200, observation()),
            (200, {'performed': True, 'pointerActions': 1}), (409, {}), (200, observation(2)), (200, {})])
        await agent.handle({'id': 'run', 'url': 'https://natalie.mu/comic'})
        record = ledger.data['runs']['run']
        assert record['reason'] == 'challenge_changed'
        assert record['pointerActions'] == 1 and agent.decide.await_count == 1
        assert agent.bridge.call_args_list[-1].args[0] == 'cancel'
    asyncio.run(run())


def test_expired_control_cannot_start_paid_request(tmp_path):
    async def run():
        ledger = Ledger(tmp_path/'state.json')
        ledger.begin('run', 'vision')
        agent = Agent(ledger, base='http://browser:3003', token='test', model_url='https://model.test', model_key='secret', model='vision')
        with pytest.raises(AgentError, match='control_deadline'):
            await agent.decide('run', observation(), 0)
        assert ledger.data['runs']['run']['modelCalls'] == 0
    asyncio.run(run())
