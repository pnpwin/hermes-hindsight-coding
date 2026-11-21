import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from test_registration import NAME, ROOT
from test_settings import SCRATCH


class ScopeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / 'coding'
        data = self.home / 'plugin-data' / NAME
        data.mkdir(parents=True)
        self.repo = base / 'project'
        self.repo.mkdir()
        self.outside = base / 'project-other'
        self.outside.mkdir()
        self.cwd = self.repo / 'nested'
        self.cwd.mkdir()
        self.alias = self.repo / 'symlink'
        self.alias.symlink_to(self.outside, target_is_directory=True)
        upstream = base / 'coding-agent.json'
        upstream.write_text('{}', encoding='utf-8')
        (data / 'config.json').write_text(json.dumps({
            'config_path': str(upstream), 'allowed_roots': [str(self.repo)],
            'allowed_api_url': 'http://127.0.0.1:8888',
        }), encoding='utf-8')
        self.kwargs: dict[str, Any] = dict(hermes_home=str(self.home), cwd=str(self.cwd), agent_identity='coding',
                           platform='tui', agent_context='primary')

    def provider(self) -> Any:
        from plugins import memory
        with patch.object(memory, 'find_provider_dir', return_value=ROOT):
            p = memory.load_memory_provider(NAME, register_skills=False)
        assert p is not None
        return p


class ScopeTest(ScopeFixture):
    def initialize(self, *, backend: str | None = 'local', **kw):
        p = self.provider()
        scope = None if backend is None else {'TERMINAL_ENV': backend}
        with (patch('tools.terminal_scope.get_terminal_scope', return_value=scope),
              patch('tools.terminal_scope.terminal_env', return_value=backend)):
            p.initialize('session-1', **(self.kwargs | kw))
        return p

    def test_only_primary_coding_workspace_can_bind_policy(self):
        p = self.initialize()
        self.assertEqual(p.session_cwd, self.cwd)
        self.assertFalse(p.settings.writes)
        self.assertIsNone(p.on_memory_write('add', 'memory', 'fixture marker'))
        self.assertIn('hindsight_diagnose', {row['function']['name'] for row in p.get_tool_schemas()})

    def test_disallowed_and_unknown_contexts_do_not_open_policy(self):
        for kw in ({'agent_identity': ''}, {'agent_context': 'subagent'},
                   {'agent_context': 'cron'}, {'agent_context': 'flush'},
                   {'platform': 'telegram'}, {'backend': 'docker'},
                   {'cwd': None}, {'cwd': '.'}, {'cwd': str(self.outside)},
                   {'cwd': str(self.alias)}):
            with self.subTest(kw=kw):
                p = self.initialize(**kw)
                self.assertIsNone(p.settings)
                self.assertIsNone(p.session_cwd)

    def test_selected_default_can_load_its_own_policy(self):
        p = self.initialize(agent_identity='default')
        self.assertIsNotNone(p.settings)
        self.assertEqual(p.session_cwd, self.cwd)

    def test_initialized_provider_does_not_rebind_to_another_home(self):
        p = self.initialize()
        with patch('tools.terminal_scope.terminal_env', return_value='local'):
            p.initialize('session-2', **(self.kwargs | {'hermes_home': str(self.outside)}))
        self.assertEqual(p.session_id, 'session-1')
        self.assertEqual(p.session_cwd, self.cwd)

    def test_plugin_host_without_rpc_policy_cannot_infer_local(self):
        with patch.dict(os.environ, {'HERMES_PLUGIN_HOST_PROCESS': '1', 'TERMINAL_ENV': 'local'}):
            p = self.initialize()
            self.assertIsNone(p.settings)
            self.assertFalse(p.is_available())

    def test_missing_terminal_scope_cannot_infer_local(self):
        self.assertIsNone(self.initialize(backend=None).settings)

    def test_terminal_policy_refusal_stays_inactive(self):
        from tools.terminal_scope import TerminalPolicyRefusal

        p = self.provider()
        with patch('tools.terminal_scope.get_terminal_scope', return_value=TerminalPolicyRefusal('fixture')):
            p.initialize('session-1', **self.kwargs)
        self.assertIsNone(p.settings)

    def test_bound_local_terminal_policy_allows_local_workspace(self):
        from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

        p = self.provider()
        token = set_terminal_scope({'TERMINAL_ENV': 'local'})
        try:
            p.initialize('session-1', **self.kwargs)
            self.assertEqual(p.session_cwd, self.cwd)
        finally:
            reset_terminal_scope(token)
