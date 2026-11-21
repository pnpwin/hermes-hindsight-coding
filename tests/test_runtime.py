"""Run integration cases in a fresh process: Hermes initialization sets process-global state."""
from pathlib import Path
import subprocess
import sys
import unittest


class RuntimeVerificationTest(unittest.TestCase):
    def test_live_modes_require_exact_typed_bank_consent_before_bootstrap(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/verify_runtime.py'
        samples = [('disposable', 'ht-hermes-parity-test-0001'), ('existing-read', 'coding-agent::project')]
        for scope, bank in samples:
            with self.subTest(scope=scope):
                result = subprocess.run([sys.executable, str(script), '--scope', scope, '--bank', bank,
                                         '--confirm-bank', 'wrong-bank'], capture_output=True, text=True, timeout=6)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--confirm-bank must exactly match --bank', result.stderr)

    def test_live_scope_dispatch_uses_only_matching_confirmation(self):
        from importlib.util import module_from_spec, spec_from_file_location
        from unittest.mock import patch

        script = Path(__file__).resolve().parents[1] / 'scripts/verify_runtime.py'
        spec = spec_from_file_location('runtime_verifier', script)
        assert spec and spec.loader
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        for scope, bank in [('disposable', 'ht-hermes-parity-test-0001'), ('existing-read', 'coding-agent::project')]:
            reservation = ['--reservation', '1'*32] if scope == 'disposable' else []
            with (self.subTest(scope=scope), patch.object(module, 'run_live', create=True) as called,
                  patch.object(sys, 'argv', [str(script), '--scope', scope, '--bank', bank, '--confirm-bank', bank, *reservation])):
                module.main()
                called.assert_called_once_with(scope, bank, *reservation[1:])

    def test_fresh_runtime_verifier(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / 'scripts/verify_runtime.py'), '--scope', 'mock'],
                                cwd=root, capture_output=True, text=True, timeout=100)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS scope=mock bank=coding-agent::project provider=hermes-hindsight-coding', result.stdout)
