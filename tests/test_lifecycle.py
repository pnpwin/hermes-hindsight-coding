import json
import os
from pathlib import Path
import sys
import threading
from contextlib import contextmanager
from unittest.mock import patch

from test_bridge_client import BUNDLE, NODE
from test_codex_oracle import init_fixture_repo
from test_scope import ScopeFixture


@contextmanager
def local_session(cwd=None):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    token = set_terminal_scope({'TERMINAL_ENV': 'local'})
    scope = set_session_cwd(str(cwd) if cwd else '')
    try:
        yield
    finally:
        reset_session_cwd(scope)
        reset_terminal_scope(token)


class LifecycleFixture(ScopeFixture):
    def setUp(self):
        super().setUp()
        settings_file = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
        data = json.loads(settings_file.read_text())
        data['allowed_roots'].append(str(self.outside))
        data['writes'] = True
        settings_file.write_text(json.dumps(data))
        Path(data['config_path']).write_text(json.dumps({
            'serverMode': 'self-hosted', 'apiUrl': data['allowed_api_url'],
            'bankIdTemplate': 'coding-agent::{gitProject}',
        }))
        init_fixture_repo(self.repo, self.home)
        init_fixture_repo(self.outside, self.home)
        self.kwargs.update(resumed=False, message_count=0)

    def activate(self, session_id='test-session', **overrides):
        p = self.provider()
        mod = sys.modules[type(p).__module__]
        from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
        token = set_terminal_scope({'TERMINAL_ENV': 'local'})
        try:
            with (patch.object(mod, 'BRIDGE', BUNDLE), patch.object(mod, '_runtime_node', return_value=NODE)):
                p.initialize(session_id, **(self.kwargs | overrides))
        finally:
            reset_terminal_scope(token)
        return p


class LifecycleTest(LifecycleFixture):
    def test_same_session_same_bank_after_cd(self):
        p = self.activate()
        self.assertEqual(p.binding.bank_id, 'coding-agent::project')
        self.assertEqual(p.binding.start_cwd, str(self.cwd))
        # In-process CWD is not an authority; Hermes sends its own session_cwd.
        with patch.object(os, 'getcwd', return_value=str(self.outside)):
            resumed = self.activate(cwd=str(self.repo), resumed=True)
        self.assertEqual(resumed.binding, p.binding)
        with local_session(self.repo):
            self.assertEqual(p.acquire_work('test-session').binding, p.binding)
        with local_session(self.outside):
            self.assertIsNone(p.acquire_work('test-session'))

    def test_resume_different_repo_is_inactive(self):
        first = self.activate()
        self.assertIsNotNone(first.binding)
        resumed = self.activate(cwd=str(self.outside), resumed=True)
        self.assertIsNone(resumed.binding)
        self.assertEqual(self.activate(resumed=True).binding, first.binding)

    def test_resume_without_prior_binding_is_not_guessed(self):
        self.assertIsNone(self.activate('old-session', resumed=True).binding)

    def test_duplicate_initialization_cannot_claim_fresh_write_permission(self):
        owner = self.activate('root')
        duplicate = self.activate('root')
        with local_session(self.cwd):
            self.assertIsNotNone(owner.acquire_work('root', writing=True))
            self.assertIsNone(duplicate.acquire_work('root', writing=True))
            self.assertEqual(duplicate.acquire_work('root').binding, owner.binding)

    def test_resume_rejects_config_change_even_if_bank_name_is_unchanged(self):
        p = self.activate()
        self.assertIsNotNone(p.binding)
        p.settings.config_path.write_text(p.settings.config_path.read_text() + '\n')
        self.assertIsNone(self.activate(resumed=True).binding)

    def test_delayed_job_cannot_use_next_bank(self):
        p = self.activate('left')
        another = self.activate('right', cwd=str(self.outside))
        from agent.memory_manager import MemoryManager
        with local_session(self.cwd):
            old = p.acquire_work('left', writing=True)
            self.assertEqual(old.binding.bank_id, 'coding-agent::project')
        with local_session(self.outside):
            manager = MemoryManager()
            manager.add_provider(p)
            manager.on_session_switch('right', reason='resume', parent_session_id='left')
            self.assertEqual(p.binding, another.binding)
            self.assertFalse(p.is_current(old))
            self.assertIsNone(p.acquire_work('left'))
            self.assertIsNone(p.acquire_work('right', writing=True))
            self.assertEqual(p.acquire_work('right').binding.bank_id, 'coding-agent::project-other')

    def test_rewind_keeps_document(self):
        p = self.activate('root')
        with local_session(self.cwd):
            old = p.acquire_work('root', writing=True)
            p.on_session_switch('root', rewound=True)
            replacement = p.acquire_work('root', writing=True)
            self.assertFalse(p.is_current(old))
            self.assertTrue(replacement.replace_snapshot)
            self.assertEqual(old.binding, replacement.binding)
            p.acknowledge_replacement(old)
            self.assertTrue(p.acquire_work('root', writing=True).replace_snapshot)
            p.acknowledge_replacement(replacement)
            self.assertFalse(p.acquire_work('root', writing=True).replace_snapshot)

    def test_branch_no_inherited_history(self):
        for reason in ('branch', 'compression'):
            with self.subTest(reason=reason), local_session(self.cwd):
                p = self.activate(reason + '-parent')
                old = p.acquire_work(reason + '-parent', writing=True)
                p.on_session_switch(reason + '-child', reason=reason, parent_session_id=reason + '-parent')
                self.assertFalse(p.is_current(old))
                child = p.acquire_work(reason + '-child')
                self.assertEqual(child.binding.bank_id, old.binding.bank_id)
                self.assertFalse(child.binding.write_eligible)
                self.assertIsNone(p.acquire_work(reason + '-child', writing=True))

    def test_switch_without_authoritative_cwd_cannot_retarget_provider(self):
        p = self.activate('left')
        self.activate('right')
        with local_session():
            p.on_session_switch('right', reason='resume')
            self.assertIsNone(p.binding)
            self.assertIsNone(p.acquire_work('right'))

    def test_policy_revocation_stops_inflight_and_new_work(self):
        p = self.activate('root')
        with local_session(self.cwd):
            old = p.acquire_work('root', writing=True)
            policy = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
            value = json.loads(policy.read_text())
            value['writes'] = False
            policy.write_text(json.dumps(value))
            self.assertFalse(p.is_current(old))
            self.assertIsNone(p.acquire_work('root', writing=True))

    def test_rejects_existing_prompt_only_history_as_fresh(self):
        p = self.activate('prompt-only', resumed=False, message_count=3)
        self.assertIsNone(p.binding)
        self.assertIsNone(self.activate('prompt-only', resumed=True).binding)

    def test_revalidates_live_workspace_and_upstream_disable(self):
        p = self.activate('root')
        with local_session(self.cwd):
            old = p.acquire_work('root', writing=True)
            self.assertIsNotNone(old)
        with local_session(self.outside):
            self.assertFalse(p.is_current(old))
        with local_session(self.cwd):
            config = p.settings.config_path
            config.write_text(json.dumps(json.loads(config.read_text()) | {'disabled': True}))
            self.assertFalse(p.is_current(old))
            self.assertIsNone(p.acquire_work('root', writing=True))
            self.assertIsNone(p.acquire_work('root'))

    def test_late_new_session_does_not_override_resume(self):
        p = self.activate('left')
        target = self.activate('right', cwd=str(self.outside))
        with local_session(self.outside):
            p.on_session_switch('right', reason='resume', parent_session_id='left')
            right = p.acquire_work('right')
            self.assertIsNotNone(right)
        with local_session(self.cwd):
            p.on_session_switch('stale', parent_session_id='left', reset=True, reason='new_session')
        with local_session(self.outside):
            self.assertEqual(p.binding, target.binding)
            self.assertTrue(p.is_current(right))
            self.assertIsNone(p.acquire_work('stale', writing=True))

    def test_reset_from_fresh_parent_but_not_after_leave_and_resume(self):
        p = self.activate('start')
        with local_session(self.cwd):
            p.on_session_switch('next', parent_session_id='start', reset=True, reason='new_session')
            self.assertIsNotNone(p.acquire_work('next', writing=True))
            p.on_session_switch('start', parent_session_id='next', reason='resume')
            p.on_session_switch('late', parent_session_id='start', reset=True, reason='new_session')
            self.assertIsNotNone(p.acquire_work('start'))
            self.assertIsNone(p.acquire_work('late'))

    def test_initialize_cannot_publish_an_old_bank_after_switch(self):
        from concurrent.futures import ThreadPoolExecutor

        target = self.activate('right', cwd=str(self.outside))
        p = self.provider()
        mod = sys.modules[type(p).__module__]
        entered, release, switching = (threading.Event() for _ in range(3))
        select = p._select_binding

        def delayed(*args, **kwargs):
            if args[0] == 'left':
                entered.set()
                if not release.wait(10):
                    raise AssertionError('initialization barrier timed out')
            return select(*args, **kwargs)

        def initialize():
            with local_session(self.cwd):
                p.initialize('left', **self.kwargs)

        def change():
            with local_session(self.outside):
                switching.set()
                p.on_session_switch('right', reason='resume', parent_session_id='left')

        with (patch.object(mod, 'BRIDGE', BUNDLE), patch.object(mod, '_runtime_node', return_value=NODE),
              patch.object(p, '_select_binding', side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool):
            init = pool.submit(initialize)
            try:
                self.assertTrue(entered.wait(6))
                switch = pool.submit(change)
                self.assertTrue(switching.wait(6))
                from concurrent.futures import TimeoutError
                try:
                    switch.result(timeout=1)
                except TimeoutError:
                    pass  # a synchronized switch waits until initialization has published
            finally:
                release.set()
            init.result(timeout=10)
            switch.result(timeout=10)
        with local_session(self.outside):
            self.assertEqual(p.binding, target.binding)
            self.assertIsNone(p.acquire_work('right', writing=True))
