"""Independent Star SDK client: discover Quantum World and call GetTask.

This example needs a running Hub and World, but no agent_runtime package.
"""
import argparse
import asyncio
import getpass
import json
import os
from uuid import uuid4

from star_protocol import AgentClient


async def run(args):
    token = getpass.getpass('Hub token: ') if args.prompt_token else os.environ.get('HUB_CLIENT_TOKEN')
    if not token:
        raise ValueError('Set HUB_CLIENT_TOKEN or pass --prompt-token')
    client = AgentClient(args.agent)
    try:
        async with asyncio.timeout(20):
            await client.connect(args.hub, headers={'Authorization': 'Bearer ' + token})
            await client.start()
            await client.join_environment(args.world)
            while client.protocol_for(args.world) != 'agent-env-compact/2':
                await asyncio.sleep(.1)
        specification = client.specification_for(args.world)
        if 'GetTask' not in {t['name'] for t in specification['tools']}:
            raise ValueError('World did not advertise GetTask')
        action_id = 'example-' + uuid4().hex
        await client.send_action(args.world, 'GetTask', {'userId': args.user, 'task': args.task}, action_id=action_id)
        outcome = await client.await_action(action_id, timeout=30)
        print(json.dumps({'status': outcome['status'], 'result': json.loads(outcome['data']['text'])}, ensure_ascii=False, indent=2))
    finally:
        await client.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub', default='ws://127.0.0.1:8000')
    parser.add_argument('--world', default='quantum-world-local')
    parser.add_argument('--agent', default='quantum-agent-local')
    parser.add_argument('--user', default='local-user')
    parser.add_argument('--task', choices=['invsqrt', 'dqi', 'dfthcsos'], default='invsqrt')
    parser.add_argument('--prompt-token', action='store_true')
    asyncio.run(run(parser.parse_args()))
