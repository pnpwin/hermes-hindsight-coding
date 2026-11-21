import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.memory_provider import MemoryProvider

ROOT = Path(__file__).resolve().parents[1]
NAME = 'hermes-hindsight-coding'


class RegistrationTest(unittest.TestCase):
    def test_register_constructs_a_fresh_provider_without_io(self):
        package = '_test_coding_hermes'
        spec = importlib.util.spec_from_file_location(package, ROOT / '__init__.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        self.addCleanup(sys.modules.pop, package, None)
        sys.modules[package] = module
        providers = []
        context = SimpleNamespace(register_memory_provider=providers.append)
        with (patch.object(subprocess, 'Popen', side_effect=AssertionError('started a process')),
              patch.object(socket, 'create_connection', side_effect=AssertionError('opened a connection'))):
            spec.loader.exec_module(module)
            module.register(context)
            module.register(context)
            provider_module = sys.modules[package + '.provider']
            with patch.object(provider_module, 'BRIDGE', ROOT / 'tests/missing-bridge.mjs'):
                self.assertFalse(providers[0].is_available())
                self.assertEqual(providers[0].get_tool_schemas(), [])
        self.assertEqual([p.name for p in providers], [NAME, NAME])
        self.assertIsInstance(providers[0], MemoryProvider)
        self.assertIsNot(providers[0], providers[1])

    def test_real_memory_loader_accepts_provider(self):
        from plugins import memory
        with patch.object(memory, 'find_provider_dir', return_value=ROOT):
            provider = memory.load_memory_provider(NAME, register_skills=False)
        self.assertIsInstance(provider, MemoryProvider)
        assert provider is not None
        self.assertEqual(provider.name, NAME)
