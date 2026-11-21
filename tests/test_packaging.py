"""Check the committed, offline-installable artifact rather than the scratch build."""
import hashlib
import json
import re
import subprocess
import unittest

from test_bridge_client import NODE
from test_settings import ROOT


class PackagingTest(unittest.TestCase):
    def test_bundle_executes_and_matches_its_manifest_and_schemas(self):
        bundle = ROOT / 'bridge/dist/hermes-bridge.mjs'
        self.assertTrue(bundle.is_file(), 'installable bundle is required')
        self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.with_suffix('.sha256').read_text().strip())
        env = {'PATH': '/usr/bin:/bin'}
        subprocess.run([str(NODE), '--check', str(bundle)], check=True, env=env, timeout=5)
        out = subprocess.check_output([str(NODE), str(bundle), '--schemas'], env=env, timeout=5)
        self.assertEqual(json.loads(out), json.loads(bundle.with_suffix('.schemas.json').read_bytes()))
        result = subprocess.run([str(NODE), str(bundle)], input=b'{}', capture_output=True, check=True, env=env, timeout=5)
        self.assertEqual(json.loads(result.stdout), {'v': 1, 'ok': False, 'error': {'code': 'INVALID_REQUEST'}})

    def test_notice_contains_the_engine_license_and_bundled_licenses(self):
        notice = (ROOT / 'engine/NOTICE.md').read_text()
        self.assertIn('Copyright (c) 2025 Vectorize AI, Inc.', notice)
        for name in ('zod', 'jsonc-parser', 'smol-toml'):
            self.assertRegex(notice, r'(?m)^### ' + re.escape(name) + r'@[^\s]+$')
