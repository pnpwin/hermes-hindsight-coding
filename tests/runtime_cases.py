"""Exercise the packaged provider through the real AIAgent in a temporary coding home."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import test_prefetch
from test_bridge_client import NODE
from test_codex_oracle import MCP
from test_lifecycle import local_session
from test_registration import NAME, ROOT
from test_tools import READ_NAMES

BANK_A = '/v1/default/banks/coding-agent%3A%3Aproject'
BANK_B = '/v1/default/banks/coding-agent%3A%3Aproject-other'


class FixtureDocuments:
    """Minimal loopback document double, not a model of Hindsight extraction or durability."""

    def __init__(self):
        self.docs = {}
        self.receipts = {}

    def retain(self, path, body):
        bank = path.removesuffix('/memories')
        operation_id = body.get('operation_id') or str(uuid4())
        operation = (bank, operation_id)
        if operation in self.receipts:
            if self.receipts[operation] != body:
                raise ValueError('fixture operation ID reused with a different payload')
        else:
            item, = body['items']
            self.docs[(bank, item['document_id'])] = item
            self.receipts[operation] = body
        return {'operation_id': operation_id}

    def read(self, path, _body):
        bank = path.removesuffix('/memories/recall').removesuffix('/reflect')
        contents = [item['content'] for (target, _), item in self.docs.items() if target == bank]
        return {'text': '\n'.join(contents)} if path.endswith('/reflect') else {'results': [{'text': text} for text in contents]}


class RuntimeTest(test_prefetch.PrefetchFixture):
    def setUp(self):
        super().setUp()
        root = self.home.parent / '.hermes'
        home = root
        self.home.rename(home)
        self.home = home

        self.enterContext(patch.dict(os.environ, {'HERMES_HOME': str(home), 'HOME': str(root.parent),
                                                'HERMES_DISABLE_UPDATE_CHECK': '1'}))
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        self.addCleanup(reset_hermes_home_override, set_hermes_home_override(home))
        (home / 'config.yaml').write_text(json.dumps({
            'memory': {'provider': NAME}, 'plugins': {'enabled': [NAME], 'isolation': 'in_process'},
            'terminal': {'backend': 'local'}, 'model': {'default': 'fixture-model'},
        }))
        self.plugin = home / 'plugins' / NAME
        for filename in ['plugin.yaml', '__init__.py', 'settings.py', 'provider.py', 'session.py', 'bridge_client.py',
                         'prefetch.py', 'retain.py', 'transcript.py', 'tools.py', 'seed.py', 'engine/NODE.json',
                         'bridge/dist/hermes-bridge.mjs', 'bridge/dist/hermes-bridge.sha256', 'bridge/dist/hermes-bridge.schemas.json']:
            target = self.plugin / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
        node = home / 'plugin-data' / NAME / 'runtime/node/bin/node'
        node.parent.mkdir(parents=True)
        shutil.copy2(NODE, node)
        self.api.bank_config = {'retain_strategies': {'conversation': {}}, 'store_document_text': True}
        self.documents = FixtureDocuments()
        self.api.retain_at_path = self.documents.retain

    def make_agent(self, cwd, sid):
        from hermes_cli.profiles import get_active_profile_name
        from run_agent import AIAgent

        self.assertEqual(get_active_profile_name(), 'default')
        with local_session(cwd), patch('agent.process_bootstrap.OpenAI'):
            agent: Any = AIAgent(api_key='fixture-key', base_url='http://127.0.0.1:1/v1', provider='custom',
                            model='fixture-model', quiet_mode=True, skip_context_files=True,
                            skip_background_review=True, enabled_toolsets=['memory'], cwd=str(cwd), session_id=sid)
        self.addCleanup(agent.close)
        self.assertIsNotNone(agent._memory_manager)
        return agent

    def invoke(self, agent, name, args):
        from agent.inline_tool_executors import InlineToolContext, resolve_invoke_tool_executor

        execute = resolve_invoke_tool_executor(agent, name)
        assert execute is not None, name
        value = json.loads(execute(agent, args, InlineToolContext('fixture')))
        self.assertNotIn('error', value)
        return value['result']

    def assert_bank(self, agent, expected):
        value = self.invoke(agent, 'hindsight_diagnose', {})
        self.assertEqual(value['bank_id'], expected)
        self.assertEqual(value['api_url'], self.api.origin)
        self.assertEqual({schema['name'] for schema in agent._memory_manager.get_all_tool_schemas()}, READ_NAMES)
        self.assertEqual({schema['function']['name'] for schema in agent.tools
                          if schema.get('function', {}).get('name', '').startswith('hindsight_')}, READ_NAMES)

    def test_packaged_provider_runs_real_agent_lifecycle(self):
        from gateway.session_context import scoped_current_session_id

        sid = 'runtime-fixture'
        agent = self.make_agent(self.cwd, sid)
        manager = agent._memory_manager
        p, = manager.providers
        self.assertEqual(p.name, NAME)
        origin = sys.modules[type(p).__module__].__file__
        assert origin is not None
        self.assertTrue(Path(origin).is_relative_to(self.plugin))
        with local_session(self.cwd), scoped_current_session_id(sid):
            self.assert_bank(agent, 'coding-agent::project')
            self.assertIn('historical project fact', manager.prefetch_all('current fixture prompt', session_id=sid))
            self.assertNotIn('historical project fact', manager.prefetch_all('next fixture prompt', session_id=sid))
            messages = [{'role': 'user', 'content': 'fixture request'}, {'role': 'assistant', 'content': 'fixture result'}]
            for _ in range(2):
                manager.sync_all('fixture request', 'fixture result', session_id=sid, messages=messages)
                self.assertTrue(manager.flush_pending(timeout=15))
            manager.shutdown_all()
        writes = [(path, body) for method, path, body in self.api.requests if method == 'POST' and path.endswith('/memories')]
        (path, request), = writes
        self.assertEqual(path, BANK_A + '/memories')
        item, = request['items']
        self.assertEqual(item['strategy'], 'conversation')
        self.assertIn('harness:hermes', item['tags'])
        self.assertIn('fixture result', item['content'])
        self.assertIsNone(p.binding)
        reopened = self.make_agent(self.cwd, sid)
        resumed, = reopened._memory_manager.providers
        self.assertEqual(resumed.binding.bank_id, 'coding-agent::project')
        self.assertFalse(resumed._fresh_writes)
        with local_session(self.cwd), scoped_current_session_id(sid):
            self.assert_bank(reopened, 'coding-agent::project')
            self.assertIn('fixture page content', json.dumps(self.invoke(reopened, 'hindsight_read_page', {'page_id': 'kp-one'})))
            reopened._memory_manager.sync_all('fixture request', 'fixture result', session_id=sid, messages=messages)
            self.assertTrue(reopened._memory_manager.flush_pending(timeout=15))
        self.assertEqual(sum(method == 'POST' and path.endswith('/memories') for method, path, _ in self.api.requests), 1)
        self.assertEqual(self.documents.docs, {(BANK_A, item['document_id']): item})

    def test_read_only_profile_reads_but_never_mutates(self):
        from agent.inline_tool_executors import resolve_invoke_tool_executor
        from gateway.session_context import scoped_current_session_id

        policy = self.home / 'plugin-data' / NAME / 'config.json'
        upstream = json.loads(policy.read_text())['config_path']
        policy.write_text(json.dumps({'config_path': upstream, 'allowed_roots': [str(self.repo), str(self.outside)],
                                      'allowed_api_url': self.api.origin, 'writes': False, 'seed': False,
                                      'survey': False, 'manage_bank_config': False}))
        sid = 'readonly-runtime-fixture'
        agent = self.make_agent(self.cwd, sid)
        manager = agent._memory_manager
        provider, = manager.providers
        with local_session(self.cwd), scoped_current_session_id(sid):
            self.assert_bank(agent, 'coding-agent::project')
            self.assertIn('historical project fact', manager.prefetch_all('synthetic readonly prompt', session_id=sid))
            self.assertIn('fixture page content', json.dumps(self.invoke(agent, 'hindsight_read_page', {'page_id': 'kp-one'})))
            self.assertIn('fixture synthesis', json.dumps(self.invoke(agent, 'hindsight_reflect', {'query': 'synthetic query'})))
            manager.sync_all('fixture question', 'fixture answer', session_id=sid, messages=[
                {'role': 'user', 'content': 'fixture question'}, {'role': 'assistant', 'content': 'fixture answer'},
            ])
            manager.queue_prefetch_all('synthetic readonly prompt', session_id=sid)
            self.assertTrue(manager.flush_pending(timeout=15))
            for name in ('hindsight_ingest_document', 'hindsight_capture_initiative', 'hindsight_retain', 'hindsight_delete_bank'):
                self.assertIsNone(resolve_invoke_tool_executor(agent, name))
                self.assertEqual(json.loads(provider.handle_tool_call(name, {'content': 'fixture write'})), {'error': 'TOOL_NOT_ALLOWED'})
            manager.shutdown_all()
        self.assertCountEqual(self.api.wire_requests, [
            ('GET', BANK_A + '/pages/tree'),
            ('POST', BANK_A + '/memories/recall'),
            ('GET', BANK_A + '/pages/kp-one'),
            ('POST', BANK_A + '/reflect'),
        ])
        self.assertEqual(self.documents.docs, {})
        self.assertFalse(list((self.home / 'plugin-data' / NAME).rglob('retain-cursors/*.json')))

    def test_real_mcp_and_hermes_observe_each_others_fixture_writes(self):
        from gateway.session_context import scoped_current_session_id

        sid = 'cross-client-fixture'
        agent = self.make_agent(self.cwd, sid)
        mgr = agent._memory_manager
        hermes_mark = 'hermes-fixture-' + uuid4().hex
        codex_mark = 'codex-fixture-' + uuid4().hex
        other_mark = 'second-bank-fixture-' + uuid4().hex
        self.documents.docs[(BANK_B, 'preset-second-bank-document')] = {'content': other_mark}
        with local_session(self.cwd), scoped_current_session_id(sid):
            mgr.sync_all('fixture question', hermes_mark, session_id=sid, messages=[
                {'role': 'user', 'content': 'fixture question'}, {'role': 'assistant', 'content': hermes_mark},
            ])
            self.assertTrue(mgr.flush_pending(timeout=15))

        self.api.read_memory = self.documents.read
        diagnosis, reflected = asyncio.run(self.codex_round_trip(codex_mark))
        self.assertEqual(diagnosis['bank_id'], 'coding-agent::project')
        self.assertIn(hermes_mark, reflected)
        with local_session(self.cwd), scoped_current_session_id(sid):
            self.assertIn(codex_mark, mgr.prefetch_all('recall the cross-client notes', session_id=sid))
        with local_session(self.outside), scoped_current_session_id('other-runtime-fixture'):
            other = self.make_agent(self.outside, 'other-runtime-fixture')
            other_manager = other._memory_manager
            self.assert_bank(other, 'coding-agent::project-other')
            before = len(self.api.requests)
            found = other_manager.prefetch_all('recall the cross-client notes', session_id='other-runtime-fixture')
            self.assertIn(other_mark, found)
            self.assertNotIn(hermes_mark, found)
            self.assertNotIn(codex_mark, found)
            self.assertEqual([path for method, path, _ in self.api.requests[before:] if method == 'POST'],
                             [BANK_B + '/memories/recall'])
        self.assertEqual({path for method, path, _ in self.api.requests if method == 'POST' and path.endswith('/memories')},
                         {BANK_A + '/memories'})
        posts = [body for method, path, body in self.api.requests if method == 'POST' and path.endswith('/memories')]
        self.assertEqual([body['items'][0]['strategy'] for body in posts], ['conversation', 'document'])
        stored = [item for (bank, _), item in self.documents.docs.items() if bank == BANK_A]
        self.assertEqual(len(stored), 2)
        self.assertTrue(stored[0]['document_id'].startswith('conversation:hermes-'))
        self.assertEqual(stored[1]['document_id'], 'cross-client-fixture')

    async def codex_round_trip(self, marker):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import TextContent

        self.assertTrue(MCP.is_file(), 'installed Coding Agents oracle is required')
        config = Path(json.loads((self.home / 'plugin-data' / NAME / 'config.json').read_text())['config_path'])
        raw = json.loads(config.read_bytes())
        self.assertEqual(raw['apiUrl'], self.api.origin)
        self.assertEqual(raw['serverMode'], 'self-hosted')
        self.assertNotIn('apiToken', raw)
        oracle = self.home / 'fixture-mcp'
        oracle.mkdir(mode=0o700)
        params = StdioServerParameters(command=str(NODE), args=[str(MCP)], cwd=str(self.cwd), env={
            'HOME': str(oracle), 'TMPDIR': str(oracle), 'PATH': '/usr/bin:/bin', 'HINDSIGHT_MCP_HARNESS': 'codex',
            'HINDSIGHT_CONFIG': str(config), 'HINDSIGHT_DISABLE_HOOKS': '1', 'HINDSIGHT_LOG_FILE': str(oracle / 'plugin.log'),
            'HINDSIGHT_DIAG_FILE': str(oracle / 'diag.jsonl'),
        })
        async with asyncio.timeout(20), stdio_client(params) as (rd, wr):
            async with ClientSession(rd, wr) as client:
                await client.initialize()
                diagnose = await client.call_tool('hindsight_diagnose', {})
                self.assertFalse(diagnose.is_error)
                info = diagnose.content[0]
                assert isinstance(info, TextContent)
                diagnosis = json.loads(info.text)
                diagnosis = diagnosis.get('data', diagnosis)
                self.assertEqual(diagnosis['config']['api_url'], self.api.origin)
                read = await client.call_tool('hindsight_reflect', {'query': 'Read the fixture conversation'})
                self.assertFalse(read.is_error)
                write = await client.call_tool('hindsight_ingest_document', {'title': 'Cross-client fixture', 'content': marker})
                self.assertFalse(write.is_error)
                reflected = read.content[0]
                assert isinstance(reflected, TextContent)
                return diagnosis, reflected.text
