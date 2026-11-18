"""Check the short-prompt World through its existing Hub, without a model or an experiment."""

import argparse
import asyncio
import json
import os
from uuid import uuid4

from star_protocol import AgentClient


WORLD = 'quantum-world-invsqrt-local'
AGENT = 'quantum-agent-local'
USER = 'local-user'
TOOLS = {'GetTask', 'SearchResearchMethods', 'GetResearchNode', 'StartInversqrtOptimization'}


async def probe(baseline_full_id, baseline_quick_id):
    token = os.environ.get('HUB_CLIENT_TOKEN')
    if not token:
        raise ValueError('Set the existing local Hub credential in the process environment')
    client = AgentClient(AGENT)

    async def call(name, **params):
        action = 'short-prompt-check-' + uuid4().hex
        await client.send_action(WORLD, name, {'userId': USER, **params}, action_id=action)
        outcome = await client.await_action(action, timeout=30)
        if outcome.get('status') != 'completed' or outcome.get('refs') != [action]:
            raise ValueError(f'Read-only {name} did not return a matching completed Outcome')
        return json.loads(outcome['data']['text'])

    try:
        async with asyncio.timeout(20):
            await client.connect('ws://127.0.0.1:28080', headers={'Authorization': 'Bearer ' + token})
            await client.start()
            await client.join_environment(WORLD)
            while (client.protocol_for(WORLD) != 'agent-env-compact/2'
                   or client.specification_for(WORLD) is None):
                await asyncio.sleep(0.1)
        discovered = {tool['name'] for tool in client.specification_for(WORLD)['tools']}
        if not TOOLS <= discovered:
            raise ValueError(f'World is missing the short-prompt tools: {sorted(TOOLS - discovered)}')

        task = await call('GetTask', task='invsqrt')
        baseline, history = task.get('baseline'), task.get('history')
        if task.get('optimization_error') or task.get('history_error'):
            errors = [task[key].get('code') for key in ('optimization_error', 'history_error') if task.get(key)]
            raise ValueError(f'World baseline or history is unavailable: {errors}')
        if not isinstance(baseline, dict) or not isinstance(history, list):
            raise ValueError('GetTask did not expose the baseline and history')
        if (baseline.get('full_id'), baseline.get('quick_id')) != (baseline_full_id, baseline_quick_id):
            raise ValueError('The live baseline pair differs from the configured receipt IDs')
        if baseline.get('parameters') != {'delta_bits': 13, 'coef_bits': 17, 'square_bits': 28,
                                          'product_bits': 27, 'result_bits': 28}:
            raise ValueError('The live baseline does not match H14 precision')

        search = await call('SearchResearchMethods', task='invsqrt', query='precision', limit=2, fallback=True)
        nodes = search.get('nodes', [])
        if not 1 <= len(nodes) <= 2:
            raise ValueError('The live graph returned no matching precision method')
        node_id = nodes[0]['id']
        detail = await call('GetResearchNode', task='invsqrt', node_id=node_id)
        if not any(node['id'] == node_id and node['source']['type'] in {'official', 'public'}
                   for node in detail.get('nodes', [])):
            raise ValueError('The live graph could not read the returned method')
        return {
            'baselineFullId': baseline_full_id, 'baselineQuickId': baseline_quick_id,
            'historyCount': len(history), 'methodNodeId': node_id, 'tools': sorted(TOOLS),
        }
    finally:
        await client.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-full-id', required=True)
    parser.add_argument('--baseline-quick-id', required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.baseline_full_id, args.baseline_quick_id)), ensure_ascii=False))
