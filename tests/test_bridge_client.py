import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_registration import ROOT
from test_settings import SCRATCH

NODE = SCRATCH / 'coding-node-22.22.3/bin/node'
BUNDLE = SCRATCH / 'hermes-hindsight-coding-bridge-087.mjs'


def bundle_digest():
    return BUNDLE.with_suffix('.sha256').read_text(encoding='ascii').strip()


def load_adapter():
    name = '_coding_bridge_fixture'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return __import__(name + '.bridge_client', fromlist=['BridgeClient'])


class BridgeClientTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / 'profile'
        self.home.mkdir()
        self.owner = self.root / 'owner'
        self.owner.mkdir()
        self.cwd = self.root / 'probe.v1'
        self.cwd.mkdir()
        self.config = self.root / 'coding-agent.json'
        self.config.write_text(json.dumps({'serverMode': 'self-hosted', 'apiUrl': 'http://127.0.0.1:1'}))

    def client(self, *, node=NODE, bundle=BUNDLE):
        module = load_adapter()
        return module.BridgeClient(
            node_path=node, bridge_path=bundle, profile_home=self.home,
            bridge_sha256=bundle_digest(),
            config_path=self.config, allowed_roots=(self.cwd,), allowed_api_url='http://127.0.0.1:1',
            routing_home=self.owner,
        )

    def test_resolves_via_verified_node_bundle_without_ambient_settings(self):
        self.assertTrue(NODE.is_file() and BUNDLE.is_file(), 'prepare pinned Node and compile the scratch bundle')
        with patch.dict(os.environ, {'NODE_OPTIONS': '--not-a-node-option', 'HINDSIGHT_BANK_ID': 'unexpected',
                                     'HINDSIGHT_CONFIG': '/should-not-be-read', 'GIT_DIR': '/also-not-read'}):
            c = self.client()
            route = c.resolve(self.cwd, self.cwd)
        self.assertIsNotNone(route)
        self.assertEqual(route['apiUrl'], 'http://127.0.0.1:1')
        self.assertEqual(route['bankId'], 'coding-agent::probe.v1')
        self.assertEqual(c.worker_home.stat().st_mode & 0o777, 0o700)
        self.assertTrue(c.worker_home.is_relative_to(self.home))

    def test_rejects_unverified_node(self):
        with self.assertRaisesRegex(load_adapter().BridgeError, 'NODE_PIN_MISMATCH'):
            self.client(node=Path(sys.executable))

    def test_bundle_link_and_changed_bundle_bytes_are_not_executed(self):
        mod = load_adapter()
        link = self.root / 'symlink.mjs'
        link.symlink_to(BUNDLE)
        with self.assertRaisesRegex(mod.BridgeError, 'BRIDGE_INTEGRITY'):
            self.client(bundle=link)
        candidate = self.root / 'candidate.mjs'
        candidate.write_text('throw Error("not the pinned bridge")')
        with self.assertRaisesRegex(mod.BridgeError, 'BRIDGE_INTEGRITY'):
            self.client(bundle=candidate)
        candidate.write_bytes(BUNDLE.read_bytes())
        client = self.client(bundle=candidate)
        candidate.write_text('throw Error("replaced after verification")')
        self.assertIsNotNone(client.resolve(self.cwd, self.cwd))

    def test_worker_directory_is_rechecked_at_spawn(self):
        mod = load_adapter()
        c = self.client()
        c.worker_home.rename(c.worker_home.with_name('old-worker'))
        c.worker_home.symlink_to(self.owner, target_is_directory=True)
        with self.assertRaisesRegex(mod.BridgeError, 'UNSAFE_WORKER_DIR'):
            c.resolve(self.cwd, self.cwd)

    def test_rejects_symlinked_worker_directory(self):
        state = self.home / 'plugin-data/hermes-hindsight-coding'
        state.mkdir(parents=True)
        (state / 'bridge-worker').symlink_to(self.owner, target_is_directory=True)
        with self.assertRaisesRegex(load_adapter().BridgeError, 'UNSAFE_WORKER_DIR'):
            self.client()

    def test_caps_output_without_returning_child_stderr(self):
        mod = load_adapter()
        with self.assertRaisesRegex(mod.BridgeError, '^WIRE_LIMIT$'):
            mod._run_bounded([sys.executable, '-I', '-c', 'import sys; sys.stderr.write("private"*200000)'],
                             b'{}', {'PATH': os.defpath}, self.root, timeout=4)

    def test_times_out_a_child_that_never_reads_its_input(self):
        mod = load_adapter()
        with self.assertRaisesRegex(mod.BridgeError, '^DEADLINE$'):
            mod._run_bounded([sys.executable, '-I', '-c', 'import time; time.sleep(10)'],
                             b'x' * 500000, {'PATH': os.defpath}, self.root, timeout=0.2)
