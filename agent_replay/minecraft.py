"""Narrow, opt-in replay of deliberately prepared Minecraft trial summaries.

Never accepts raw agent logs. Unknown keys fail closed, rather than getting copied into
an `unknown` event as the Claude transcript parser does. See docs/minecraft-trials.md.
"""
from __future__ import annotations

import json
import math
import re
from .parse import Event

MAX_BYTES = 1_000_000
KINDS = {'observe', 'look', 'look_at', 'dig', 'move', 'move_to', 'finish'}
PARAMETERS = {
    'observe': set(), 'look': {'yaw', 'pitch'}, 'look_at': {'x', 'y', 'z'},
    'dig': set(), 'move': {'direction', 'ticks'}, 'move_to': {'x', 'y', 'z'},
    'finish': set(),
}


def obj(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise ValueError('missing required trial fields')
    if value.keys() - (set(required) | set(optional)):
        raise ValueError('unsupported trial fields; prepare a minimal reviewed summary')
    return value


def choice(value, values):
    if not isinstance(value, str) or value not in values:
        raise ValueError('unsupported trial value')
    return value


def number(value):
    if type(value) not in (int, float) or abs(value) > 30_000_000 or not math.isfinite(value):
        raise ValueError('invalid numeric value')
    return value


def position(value):
    obj(value, {'x', 'y', 'z'})
    for v in value.values():
        number(v)
    return value


def items(value, *, dropped=False):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError('invalid item list')
    for item in value:
        obj(item, {'name', 'count', 'position'} if dropped else {'name', 'count'})
        if not isinstance(item['name'], str) or not re.fullmatch(r'[a-z0-9_:]{1,64}', item['name']):
            raise ValueError('invalid item identifier')
        if type(item['count']) is not int or not 0 <= item['count'] <= 2304:
            raise ValueError('invalid item count')
        if dropped:
            position(item['position'])
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate trial field')
        result[key] = value
    return result


def parse_trial(path):
    with open(path, 'rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('trial exceeds one-megabyte limit')
    trial = json.loads(data, object_pairs_hook=unique_object)
    obj(trial, {'schema', 'provenance', 'outcome', 'actions'})
    choice(trial['schema'], {'minecraft-replay-v1'})
    provenance = choice(trial['provenance'], {'synthetic', 'reviewed-derived'})
    outcome = trial['outcome']
    obj(outcome, {'infrastructure', 'acquired'})
    choice(outcome['infrastructure'], {'qualified', 'failed', 'unknown'})
    if outcome['acquired'] is not None and type(outcome['acquired']) is not bool:
        raise ValueError('acquired must be true, false or null')
    if not isinstance(trial['actions'], list) or not 1 <= len(trial['actions']) <= 256:
        raise ValueError('trial needs one to 256 action/reply pairs')

    label = ('SYNTHETIC ILLUSTRATION — not a real model run or benchmark result.'
             if provenance == 'synthetic' else
             'REVIEWED DERIVED SUMMARY — supplied claims, not independently verified by this viewer.')
    events = [Event(0, 'system', 'system', '', text=label)]
    for index, pair in enumerate(trial['actions'], 1):
        obj(pair, {'action', 'reply'})
        action, reply = pair['action'], pair['reply']
        obj(action, {'kind'}, {'yaw', 'pitch', 'x', 'y', 'z', 'direction', 'ticks'})
        kind = choice(action['kind'], KINDS)
        if set(action) - {'kind'} != PARAMETERS[kind]:
            raise ValueError('wrong action parameters')
        for key, value in action.items():
            if key == 'direction':
                choice(value, {'forward', 'back', 'left', 'right'})
            elif key != 'kind':
                number(value)
        obj(reply, {'status'}, {'observation', 'error'})
        status = choice(reply['status'], {'completed', 'rejected', 'finished'})
        if 'error' in reply:
            obj(reply['error'], {'code', 'recoverable'})
            code = reply['error']['code']
            if not isinstance(code, str) or not re.fullmatch(r'[a-z_]{1,64}', code):
                raise ValueError('invalid error code')
            if type(reply['error']['recoverable']) is not bool:
                raise ValueError('invalid recoverability')
        if 'observation' in reply:
            obs = obj(reply['observation'], {}, {'position', 'inventory', 'nearbyDroppedItems'})
            if 'position' in obs:
                position(obs['position'])
            if 'inventory' in obs:
                items(obs['inventory'])
            if 'nearbyDroppedItems' in obs:
                items(obs['nearbyDroppedItems'], dropped=True)
        events.append(Event(len(events), 'tool_use', 'assistant', '', tool=kind,
                            tool_input=action, tool_id=str(index)))
        events.append(Event(len(events), 'tool_result', 'tool', '', tool=kind,
                            tool_id=str(index), text=json.dumps(reply, indent=2),
                            is_error=status == 'rejected' or 'error' in reply))

    if outcome['infrastructure'] == 'qualified' and outcome['acquired'] is True:
        result = 'Supplied independent endpoint: acquired. Infrastructure: qualified.'
    elif outcome['acquired'] is False:
        result = 'Supplied independent endpoint: NOT acquired.'
    else:
        result = 'Collection NOT established by a qualified independent endpoint.'
    result += (' Action completion and participant inventory alone are not success evidence. '
               'Token use and cost are not supplied. Review before sharing; no automated '
               'secret scanner can establish permission or remove all personal information.')
    events.append(Event(len(events), 'system', 'system', '', text=result,
                        is_error=not (outcome['infrastructure'] == 'qualified' and outcome['acquired'] is True)))
    return events, {'lines': len(trial['actions']), 'events': len(events), 'unparsable': 0,
                    'ignored': 0, 'sidechain': 0, 'provenance': provenance, 'notice': label}
