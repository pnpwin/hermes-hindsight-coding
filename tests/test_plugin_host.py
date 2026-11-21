"""Pinned Hermes plugin-host RPC regression; no Hindsight endpoint is called."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from test_registration import ROOT
from test_settings import SCRATCH


def _close_fixture_host(host):
    # Hermes 0.23.3 shuts down the process but leaves its stderr pipe open.
    proc = host._proc
    host.shutdown()
    if proc is not None and proc.stderr is not None:
        proc.stderr.close()


class PluginHostTest(unittest.TestCase):
    def test_out_of_process_provider_refuses_backend_without_rpc_proof(self):
        from hermes_cli.plugin_host import PluginHost
        from tools.terminal_scope import TerminalPolicyRefusal, reset_terminal_scope, set_terminal_scope

        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='coding-rpc-') as tmp:
            home = Path(tmp)
            host = PluginHost(SimpleNamespace(home_path=home, _plugins={}))
            self.addCleanup(_close_fixture_host, host)
            for policy in ({'TERMINAL_ENV': 'docker'}, TerminalPolicyRefusal('fixture')):
                with self.subTest(policy=type(policy).__name__):
                    token = set_terminal_scope(policy)
                    try:
                        p = host.load_instance(
                            ROOT, module_name='_hindsight_coding_rpc_test',
                            base_ref='agent.memory_provider:MemoryProvider', capture='register_memory_provider',
                        )
                        self.assertIsNotNone(p)
                        self.assertFalse(p.is_available())
                        self.assertIn('does not forward the session terminal policy', p.unavailable_reason())
                        p.initialize('test-session', hermes_home=str(home), cwd=str(home), agent_identity='coding',
                                     platform='tui', agent_context='primary')
                        self.assertEqual(p.get_tool_schemas(), [])
                    finally:
                        reset_terminal_scope(token)
            _close_fixture_host(host)
