import json
from pathlib import Path
import subprocess

import test_prefetch
from test_lifecycle import local_session


class SeedTest(test_prefetch.PrefetchFixture):
    def git(self, *args):
        subprocess.run(['/usr/bin/git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', *args], check=True, capture_output=True,
                       env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})

    def prepare(self, mode='message', seed=True):
        policy = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
        raw = json.loads(policy.read_text())
        policy.write_text(json.dumps(raw | {'seed': seed, 'seed_bank_ids': ['coding-agent::project'] if seed else []}))
        path = Path(raw['config_path'])
        path.write_text(json.dumps(json.loads(path.read_text()) | {'autoSeed': True, 'gitIngest': mode, 'seedLimit': 10}))
        self.api.bank_config = {'retain_strategies': {'gitlog': {}}}
        self.api.retain_response = lambda body: {'operation_id': body['operation_id']}
        return self.activate('seed-fixture')

    def launch(self, provider, session_id='seed-fixture'):
        from agent.memory_manager import MemoryManager
        mgr = MemoryManager()
        mgr.add_provider(provider)
        with local_session(self.cwd):
            mgr.queue_prefetch_all('fixture prompt', session_id=session_id)
            self.assertTrue(mgr.flush_pending(timeout=19))

    def writes(self):
        return [body for method, path, body in self.api.requests if method == 'POST' and path.endswith('/memories')]

    def test_seed_same_head_once_and_new_head_same_doc_without_diff(self):
        p = self.prepare()
        (self.repo / 'fixture.txt').write_text('source contents not for extraction')
        self.git('add', 'fixture.txt'); self.git('commit', '-qm', 'First example')
        self.launch(p); self.launch(p)
        first, = self.writes()
        self.assertIn('First example', first['items'][0]['content'])
        self.assertNotIn('source contents not for extraction', first['items'][0]['content'])
        self.assertEqual(first['items'][0]['strategy'], 'gitlog')
        self.git('commit', '-qm', 'Second example', '--allow-empty')
        self.launch(p)
        _, second = self.writes()
        self.assertIn('Second example', second['items'][0]['content'])
        self.assertEqual(first['items'][0]['document_id'], 'gitlog:project')
        self.assertEqual(first['items'][0]['document_id'], second['items'][0]['document_id'])
        self.assertNotEqual(first['operation_id'], second['operation_id'])

    def test_seed_off_none_and_full_perform_no_http(self):
        from unittest.mock import patch

        self.git('commit', '-qm', 'Not for seed', '--allow-empty')
        for enabled, mode in ((False, 'message'), (True, 'none'), (True, 'full')):
            with self.subTest(enabled=enabled, mode=mode):
                p = self.prepare(mode, enabled)
                with patch.object(p._bridge, '_request', wraps=p._bridge._request) as transport:
                    self.launch(p)
                transport.assert_not_called()
                self.assertEqual(self.api.requests, [])

    def test_project_switch_does_not_export_other_git_history(self):
        from unittest.mock import patch
        from agent.memory_manager import MemoryManager

        p = self.prepare()
        self.git('commit', '-qm', 'Bound repository', '--allow-empty')
        manager = MemoryManager()
        manager.add_provider(p)
        with patch.object(p._bridge, '_request', wraps=p._bridge._request) as transport, local_session(self.outside):
            manager.queue_prefetch_all('fixture prompt', session_id='seed-fixture')
            self.assertTrue(manager.flush_pending(timeout=10))
        self.assertTrue(all(call.args[0] == 'resolve' for call in transport.call_args_list))
        self.assertEqual(self.api.requests, [])

    def test_existing_codex_document_is_not_overwritten(self):
        p = self.prepare()
        self.git('commit', '-qm', 'Separate ownership', '--allow-empty')
        self.api.seed_document = {'id': 'gitlog:project', 'tags': ['source:git', 'source:git-log']}
        self.launch(p)
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(self.api.requests[0][0], 'GET')
        self.assertEqual(self.writes(), [])

    def test_pending_head_defers_newer_head(self):
        p = self.prepare()
        self.git('commit', '-qm', 'first', '--allow-empty')
        self.launch(p)
        self.api.operation_status = 'processing'
        self.git('commit', '-qm', 'later', '--allow-empty')
        self.launch(p)
        self.assertEqual(len(self.writes()), 1)
        self.api.operation_status = 'completed'
        self.launch(p)
        self.assertEqual(len(self.writes()), 2)

    def test_session_change_after_preview_prevents_send(self):
        from unittest.mock import patch

        p = self.prepare()
        self.git('commit', '-qm', 'scope fixture', '--allow-empty')
        old = p._bridge._request
        def intercept(op, args, **kwargs):
            result = old(op, args, **kwargs)
            if op == 'seed' and args['digest'] is None:
                p.on_session_switch('different', reason='resume')
            return result
        with patch.object(p._bridge, '_request', side_effect=intercept):
            self.launch(p)
        self.assertEqual(self.api.requests, [])

    def test_session_change_during_operation_lookup_prevents_post(self):
        p = self.prepare()
        self.git('commit', '-qm', 'first', '--allow-empty')
        self.launch(p)
        self.git('commit', '-qm', 'second', '--allow-empty')
        hits = []
        def revoke(operation_id):
            hits.append(operation_id)
            p.on_session_switch('different', reason='resume')
            return 200
        self.api.operation_lookup = revoke
        self.launch(p)
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(self.writes()), 1)
        claim = next(p._bridge.worker_home.glob('seed-state/*.json'))
        self.assertEqual(json.loads(claim.read_text())['operationId'], self.writes()[0]['operation_id'])

    def test_generation_change_during_final_lease_validation_prevents_grant(self):
        from unittest.mock import patch

        p = self.prepare()
        self.git('commit', '-qm', 'fixture', '--allow-empty')
        original = p.is_current
        checks = []
        def revoke_after_check(*args, **kwargs):
            current = original(*args, **kwargs)
            checks.append(current)
            if len(checks) == 2:  # preview check, then the pre-POST handshake
                p.on_session_switch('different', reason='resume')
            return current
        with patch.object(p, 'is_current', side_effect=revoke_after_check):
            self.launch(p)
        self.assertEqual(checks, [True, True])
        self.assertEqual(self.writes(), [])

    def test_seed_grant_is_sent_under_the_session_switch_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from socket import socket
        from unittest.mock import patch

        p = self.prepare()
        self.git('commit', '-qm', 'fixture grant', '--allow-empty')
        original = socket.sendall
        can_switch = []

        def check_lock():
            acquired = p._lock.acquire(blocking=False)
            if acquired:
                p._lock.release()
            return acquired

        def observe(channel, data, *args, **kwargs):
            if data == b'+':
                can_switch.append(pool.submit(check_lock).result(timeout=2))
            return original(channel, data, *args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(socket, 'sendall', new=observe):
            self.launch(p)
        self.assertEqual(can_switch, [False])
        self.assertEqual(len(self.writes()), 1)

    def test_repo_name_is_checked_even_with_clean_commit_text(self):
        token = 'sk-proj-' + 'cD123efGH5' * 8
        path = self.repo.with_name(token)
        self.repo.rename(path)
        self.cwd = path / self.cwd.relative_to(self.repo)
        policy = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
        raw = json.loads(policy.read_text())
        raw['allowed_roots'] = [str(path) if v == str(self.repo) else v for v in raw['allowed_roots']]
        policy.write_text(json.dumps(raw))
        self.repo = path
        self.kwargs['cwd'] = str(self.cwd)
        self.prepare()
        config = Path(raw['config_path'])
        config.write_text(json.dumps(json.loads(config.read_text()) | {'bankIdTemplate': 'coding-agent::project'}))
        p = self.activate('seed-fixture-name')
        self.assertIsNotNone(p.binding)
        self.git('commit', '-qm', 'Clean example', '--allow-empty')
        self.launch(p, session_id='seed-fixture-name')
        self.assertEqual(self.api.requests, [])

    def test_explicit_bad_request_does_not_block_later_head(self):
        self.assert_rejected_post_allows_later_head(400)

    def test_explicit_unauthorized_does_not_block_later_head(self):
        self.assert_rejected_post_allows_later_head(401)

    def assert_rejected_post_allows_later_head(self, status):
        p = self.prepare()
        self.git('commit', '-qm', 'first', '--allow-empty')
        self.api.retain_response = lambda _: (status, {}, {'detail': 'fixture rejected'})
        self.launch(p)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(list(p._bridge.worker_home.glob('seed-state/*.json')), [])
        self.api.operation_status = 'not_found'
        self.git('commit', '-qm', 'second', '--allow-empty')
        self.api.retain_response = lambda body: {'operation_id': body['operation_id']}
        self.launch(p)
        self.assertEqual(len(self.writes()), 2)
        self.assertIn('second', self.writes()[-1]['items'][0]['content'])

    def test_missing_operation_on_same_head_reuses_lost_receipt_id(self):
        p = self.prepare()
        self.git('commit', '-qm', 'first', '--allow-empty')
        self.api.retain_response = lambda _: {}
        self.launch(p)
        self.assertEqual(len(self.writes()), 1)
        self.api.operation_status = 'not_found'
        self.api.retain_response = lambda body: {'operation_id': body['operation_id']}
        self.launch(p)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.writes()[0], self.writes()[1])

    def test_no_seed_without_git_history_or_with_unscrubbed_secret(self):
        p = self.prepare()
        self.launch(p)
        self.assertEqual(self.api.requests, [])
        token = 'sk-proj-' + 'cD123efGH5' * 8
        self.git('commit', '-qm', f'Remove token {token}', '--allow-empty')
        self.launch(p)
        self.assertEqual(self.api.requests, [])
