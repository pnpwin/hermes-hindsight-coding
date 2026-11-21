import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import test_prefetch
from test_bridge_client import BUNDLE
from test_lifecycle import local_session


READ_NAMES = {'hindsight_diagnose', 'hindsight_list_pages', 'hindsight_search_pages',
              'hindsight_read_page', 'hindsight_reflect', 'hindsight_sync_status'}


class ToolTest(test_prefetch.PrefetchFixture):
    def manager(self, provider):
        from agent.memory_manager import MemoryManager
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        self.addCleanup(reset_hermes_home_override, set_hermes_home_override(self.home))
        manager = MemoryManager()
        module = sys.modules[type(provider).__module__]
        with patch.object(module, 'BRIDGE', BUNDLE):
            schemas = provider.get_tool_schemas()
            manager.add_provider(provider)
        manager.initialize_all(provider.session_id, **self.kwargs)
        return manager, schemas

    def test_tools_through_hermes_inline_dispatch(self):
        from agent.inline_tool_executors import InlineToolContext, resolve_invoke_tool_executor
        from gateway.session_context import scoped_current_session_id

        p = self.activate('tool-session')
        manager, schemas = self.manager(p)
        self.assertEqual({s['function']['name'] for s in schemas}, READ_NAMES)
        agent = SimpleNamespace(_memory_manager=manager)
        cases = {
            'hindsight_diagnose': ({}, 'bank_id'),
            'hindsight_list_pages': ({}, 'items'),
            'hindsight_search_pages': ({'query': 'architecture'}, 'pages'),
            'hindsight_read_page': ({'page_id': 'kp-one'}, 'fixture page content'),
            'hindsight_reflect': ({'query': 'why'}, 'fixture synthesis'),
            'hindsight_sync_status': ({}, 'gitlogPresent'),
        }
        with scoped_current_session_id('tool-session'), local_session(self.cwd):
            for name, (args, marker) in cases.items():
                with self.subTest(name=name):
                    invoke = resolve_invoke_tool_executor(agent, name)
                    self.assertIsNotNone(invoke)
                    assert invoke is not None
                    reply = json.loads(invoke(agent, args, InlineToolContext('fixture')))
                    self.assertNotIn('error', reply)
                    self.assertIn(marker, json.dumps(reply['result']))
        self.assertTrue(self.api.requests)
        self.assertTrue(all('/banks/coding-agent%3A%3Aproject/' in path for _, path, _ in self.api.requests))

    def test_direct_writes_and_unproven_session_do_not_dispatch(self):
        from gateway.session_context import scoped_current_session_id

        p = self.activate('tool-session')
        with local_session(self.cwd), scoped_current_session_id('old-session'):
            self.assertIn('error', json.loads(p.handle_tool_call('hindsight_list_pages', {})))
        with local_session(self.cwd), scoped_current_session_id(''), patch.dict(os.environ, {'HERMES_SESSION_ID': 'tool-session'}):
            self.assertEqual(json.loads(p.handle_tool_call('hindsight_list_pages', {}))['error'], 'TOOL_SCOPE_UNCONFIRMED')
        with local_session(self.cwd), scoped_current_session_id('tool-session'):
            for name, args in [('hindsight_ingest_document', {'title': 'test', 'content': 'test'}),
                               ('hindsight_capture_initiative', {}), ('hindsight_delete_bank', {}),
                               ('hindsight_list_pages', {'bank_id': 'other'}),
                               ('hindsight_read_page', {'page_id': '../config'}),
                               ('hindsight_read_page', {'page_id': 'tree'}),
                               ('hindsight_read_page', {'page_id': 'search'}),
                               ('hindsight_read_page', {'page_id': 'export'}),
                               ('hindsight_search_pages', {'query': 'check', 'url': 'http://127.0.0.1:1'})]:
                with self.subTest(name=name):
                    code = json.loads(p.handle_tool_call(name, args))['error']
                    expected = 'INVALID_TOOL_ARGUMENTS' if name in READ_NAMES else 'TOOL_NOT_ALLOWED'
                    self.assertEqual(code, expected)
        self.assertEqual(self.api.requests, [])

    def test_sync_status_does_not_claim_malformed_operations_are_drained(self):
        from gateway.session_context import scoped_current_session_id

        self.api.documents = [{'id': 'gitlog:project'}]
        p = self.activate('tool-session')
        for value, synced, count in [([], True, 0), ({'status': 'pending'}, False, None),
                                     ([{'id': 'op-pending', 'status': 'pending'}], False, 1)]:
            self.api.operations = {'operations': value}
            with local_session(self.cwd), scoped_current_session_id('tool-session'):
                result = json.loads(p.handle_tool_call('hindsight_sync_status', {}))['result']
            self.assertEqual((result['synced'], result['activeOps']), (synced, count))

    def test_sync_status_accounts_for_the_whole_operations_listing(self):
        from gateway.session_context import scoped_current_session_id

        self.api.documents = [{'id': 'gitlog:project'}]
        p = self.activate('tool-session')
        rows = [{'id': f'op-{i}', 'status': 'completed'} for i in range(101)]

        def status():
            with local_session(self.cwd), scoped_current_session_id('tool-session'):
                result = json.loads(p.handle_tool_call('hindsight_sync_status', {}))['result']
            return result['synced'], result['activeOps']

        self.api.operation_rows = rows
        self.assertEqual(status(), (True, 0))
        pages = [path.rsplit('?', 1)[-1] for _, path, _ in self.api.requests if '/operations?' in path]
        self.assertEqual(pages, ['limit=100&offset=0', 'limit=100&offset=100'])
        rows[-1]['status'] = 'pending'
        self.assertEqual(status(), (False, 1))
        rows[-1] = rows[0].copy()  # duplicate across pages cannot establish coverage
        self.assertEqual(status(), (False, None))
        self.api.operation_rows = None
        for total in (None, 1, 401):
            with self.subTest(total=total):
                self.api.operations = {'total': total, 'operations': []}
                self.assertEqual(status(), (False, None))
        self.api.operations = {'total': 21, 'operations': rows[:20]}
        self.assertEqual(status(), (False, None))
