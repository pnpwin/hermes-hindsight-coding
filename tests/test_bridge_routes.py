import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_bridge_client import BUNDLE, NODE, bundle_digest, load_adapter
from test_codex_oracle import ORACLE_URL, diagnose, init_fixture_repo
from test_settings import SCRATCH


class BridgeRoutesTest(unittest.TestCase):
    def test_opted_in_home_is_an_exact_cwd_not_a_subtree(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='coding-home-route-') as temporary:
            base = Path(temporary).resolve()
            home, worker, allowed, denied = (base/'Operator.Home', base/'profile', base/'Operator.Home/allowed', base/'Operator.Home/denied')
            for path in (home, worker, allowed, denied):
                path.mkdir()
            config = base/'coding-agent.json'
            config.write_text(json.dumps({'serverMode':'self-hosted', 'apiUrl':ORACLE_URL,
                                          'bankIdTemplate':'coding-agent::{gitProject}'}))
            module = load_adapter()
            bridge = module.BridgeClient(node_path=NODE, bridge_path=BUNDLE, profile_home=worker,
                bridge_sha256=bundle_digest(), config_path=config, allowed_roots=(home, allowed),
                allowed_api_url=ORACLE_URL, routing_home=home)
            oracle = asyncio.run(diagnose(home, config))
            self.assertEqual(oracle, 'coding-agent::Operator.Home')
            route = bridge.resolve(home, home)
            self.assertIsNotNone(route, 'explicit HOME route must resolve')
            self.assertEqual(route['bankId'], oracle)
            self.assertIsNone(route['projectRoot'])
            self.assertIsNone(bridge.resolve(denied, home))
            self.assertIsNone(bridge.resolve(denied, denied))
            self.assertIsNone(bridge.resolve(allowed, home))
            self.assertIsNone(bridge.resolve(home, allowed))
            self.assertEqual(bridge.resolve(allowed, allowed)['bankId'], 'coding-agent::allowed')
            (home/'.git').symlink_to(denied, target_is_directory=True)
            self.assertIsNone(bridge.resolve(home, home))

    def test_pinned_bridge_matches_installed_codex_oracle(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='coding-parity-') as temporary:
            root = Path(temporary).resolve()
            home, owner, repo, plain, bare, wt = (root / name for name in
                ('hermes-home', 'operator-home', 'Repo.v1', 'Plain.v1', 'BARE.git', 'linked'))
            for p in (home, owner, repo, plain, bare):
                p.mkdir()
            nested, plain_sub = repo / 'nested', plain / 'nested'
            git = shutil.which('git')
            assert git is not None
            fixture_env = {'PATH': os.defpath, 'HOME': str(owner), 'GIT_CONFIG_NOSYSTEM': '1',
                           'GIT_CONFIG_GLOBAL': os.devnull}

            def command(cwd, *args):
                subprocess.run([git, '-C', str(cwd), *args], env=fixture_env, check=True, timeout=10,
                               capture_output=True)

            init_fixture_repo(repo, owner)
            command(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture', '--allow-empty')
            command(repo, 'worktree', 'add', '--detach', '-q', str(wt))
            command(bare, 'init', '-q', '--template=', '--bare')
            nested.mkdir()
            plain_sub.mkdir()
            config = root / 'coding-agent.json'
            base = {'serverMode': 'self-hosted', 'apiUrl': ORACLE_URL,
                    'bankIdTemplate': 'coding-agent::{gitProject}', 'dynamicBankId': True,
                    'autoSeed': False, 'retainSessions': False, 'codebaseSurvey': False}
            builder = load_adapter()
            bridge = builder.BridgeClient(node_path=NODE, bridge_path=BUNDLE, profile_home=home,
                bridge_sha256=bundle_digest(),
                config_path=config, allowed_roots=(repo, plain, bare, wt), allowed_api_url=ORACLE_URL,
                routing_home=owner)
            cases = [
                ('repo', repo, repo, repo, {}),
                ('git child', nested, nested, nested, {}),
                ('linked', wt, wt, wt, {}),
                ('standalone bare', bare, bare, bare, {}),
                ('non-Git', plain, plain, plain, {}),
                ('started non-Git', plain_sub, plain, plain, {}),
                ('main checkout mapping', wt, wt, wt, {'optInOnly': True, 'mapPathToBank': {str(repo): 'mapped::one'}}),
                ('bank alias', repo, repo, repo, {'banks': {'coding-agent::Repo.v1': {'bank': 'alias::one'}}}),
            ]
            for label, cwd, start, oracle_cwd, extra in cases:
                with self.subTest(case=label):
                    config.write_text(json.dumps(base | extra), encoding='utf-8')
                    answer = bridge.resolve(cwd, start)
                    self.assertIsNotNone(answer)
                    assert answer is not None
                    self.assertEqual(answer['bankId'], asyncio.run(diagnose(oracle_cwd, config)))
            config.write_text(json.dumps(base), encoding='utf-8')
            self.assertIsNone(bridge.resolve(plain, repo))
            self.assertIsNone(bridge.resolve(bare, repo))
            self.assertIsNone(bridge.resolve(owner, owner))

    def test_allowlist_does_not_follow_replaced_root(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            root = Path(temporary).resolve()
            owner, approved, outside = (root / n for n in ('operator', 'approved', 'outside'))
            for path in (owner, approved, outside):
                path.mkdir()
            config = root / 'coding-agent.json'
            config.write_text(json.dumps({'serverMode': 'self-hosted', 'apiUrl': ORACLE_URL}))
            module = load_adapter()
            bridge = module.BridgeClient(node_path=NODE, bridge_path=BUNDLE, profile_home=owner,
                bridge_sha256=bundle_digest(),
                config_path=config, allowed_roots=(approved,), allowed_api_url=ORACLE_URL,
                routing_home=owner)
            self.assertIsNotNone(bridge.resolve(approved, approved))
            approved.rename(root / 'old-approved')
            approved.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(module.BridgeError, 'SCOPE_CHANGED'):
                bridge.resolve(approved, approved)
