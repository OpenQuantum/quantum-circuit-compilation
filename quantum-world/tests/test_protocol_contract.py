"""Public SDK/World contract tests; no agent runtime source dependency."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from star_protocol import validate_compact_content
from quantum_world.config import Settings
from quantum_world.protocol import QuantumEnvironment
from quantum_world.service import World
from quantum_world.tools import DEFINITIONS


class ProtocolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.world = World(Settings(root, root / 'state', Path('/unused'), root,
                                   allowed_agents={'external-agent': ['user']}))
        self.endpoint = QuantumEnvironment(self.world)
        self.endpoint.send_outcome = AsyncMock()

    async def asyncTearDown(self):
        await self.world.close()
        self.temp.cleanup()

    async def invoke(self, action):
        validate_compact_content('action', action)
        await self.endpoint.on_action('external-agent', action)
        sender, outcome = self.endpoint.send_outcome.call_args.args
        self.assertEqual(sender, 'external-agent')
        validate_compact_content('outcome', outcome)
        self.assertEqual(set(outcome['data']), {'text'})
        self.assertEqual(outcome['refs'], [action['id']])
        return outcome, json.loads(outcome['data']['text'])

    async def test_public_capability_catalog(self):
        self.endpoint.send = AsyncMock()
        await self.endpoint.send_specification('external-agent')
        envelope = self.endpoint.send.call_args.args[0]
        validate_compact_content('specification', envelope.payload.content)
        self.assertEqual(envelope.payload.content['tools'], DEFINITIONS)
        self.assertNotIn('SubmitSubmission', {d['name'] for d in envelope.payload.content['tools']})

    async def test_build_submission_advertises_one_successful_full_per_benchmark(self):
        self.endpoint.send = AsyncMock()
        await self.endpoint.send_specification('external-agent')
        tools = self.endpoint.send.call_args.args[0].payload.content['tools']
        description = next(tool['description'] for tool in tools if tool['name'] == 'BuildSubmission')
        self.assertRegex(description, r'(?i)\bexactly one\b[^.]*\b(?:successful|passed)\b[^.]*\bfull\b[^.]*\b(?:each|per)\b')
        for task in ('invsqrt', 'dqi', 'dfthcsos'):
            with self.subTest(task=task):
                self.assertRegex(description, rf'\b{task}\b')

    async def test_external_action_and_terminal_replay(self):
        action = {'id': 'read-task', 'name': 'GetTask', 'params': {'userId': 'user', 'task': 'invsqrt'}}
        first, data = await self.invoke(action)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(data['task'], 'invsqrt')
        self.assertTrue(data['approval_required'])
        replay, _ = await self.invoke(action)
        self.assertEqual(first, replay)

    async def test_external_user_is_checked_by_world(self):
        outcome, data = await self.invoke({'id': 'forbidden', 'name': 'GetTask',
                                          'params': {'userId': 'other', 'task': 'invsqrt'}})
        self.assertEqual(outcome['status'], 'rejected')
        self.assertEqual(data['error']['code'], 'FORBIDDEN')

    async def test_experiments_are_polled_through_the_interface(self):
        # No worker is allowed to run: cancel the queued experiment immediately.
        outcome, data = await self.invoke({'id': 'queue', 'name': 'RunExperiment',
            'params': {'userId': 'user', 'task': 'invsqrt', 'mode': 'quick'}})
        self.assertEqual(data['status'], 'queued')
        self.assertFalse(data['full_verification_complete'])
        await self.invoke({'id': 'cancel', 'name': 'CancelExperiment',
            'params': {'userId': 'user', 'experiment_id': data['experiment_id']}})
        _, report = await self.invoke({'id': 'poll', 'name': 'GetExperiment',
            'params': {'userId': 'user', 'experiment_id': data['experiment_id']}})
        self.assertEqual(report['status'], 'cancelled')
