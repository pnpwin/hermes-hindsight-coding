import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

NODE = shutil.which('node')
MCP = Path.home() / '.hindsight/coding-agents/dist/mcp-server.js'
ORACLE_URL = 'http://127.0.0.1:1'
HERMES_SCRATCH = Path.home() / '.hermes/cache/scratch'
FIXTURES = Path(os.environ.get('HINDSIGHT_ORACLE_SCRATCH', HERMES_SCRATCH))


def init_fixture_repo(repo, root):
    env = {
        'HOME': str(root), 'PATH': os.defpath,
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    }
    subprocess.run([shutil.which('git'), 'init', '-q', '--template=', str(repo)],
                   env=env, check=True, timeout=10)


async def diagnose(cwd, config, entry=MCP):
    home = config.parent / 'mcp-home'
    home.mkdir(mode=0o700, exist_ok=True)
    diagnostics = home / 'diagnostics.jsonl'
    params = StdioServerParameters(
        command=NODE, args=[str(entry)], cwd=cwd,
        env={
            'HOME': str(home), 'TMPDIR': str(home), 'PATH': os.defpath,
            'HINDSIGHT_MCP_HARNESS': 'codex', 'HINDSIGHT_CONFIG': str(config),
            'HINDSIGHT_LOG_FILE': str(home / 'plugin.log'),
            'HINDSIGHT_DIAG_FILE': str(diagnostics),
        },
    )
    async with asyncio.timeout(10):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool('hindsight_diagnose', {})
    if reply.is_error:
        raise AssertionError('MCP diagnose failed')
    value = json.loads(reply.content[0].text)
    result = value.get('data', value)
    if result['config']['path'] != str(config) or result['config']['api_url'] != ORACLE_URL:
        raise AssertionError('MCP used a config or endpoint outside the oracle fixture')
    if result['environment']['diagnostics_file'] != str(diagnostics):
        raise AssertionError('MCP diagnostics escaped the oracle fixture')
    return result['bank_id']


class CodexOracle(unittest.TestCase):
    def test_git_fixture_ignores_inherited_git_dir(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=FIXTURES) as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            outside = root / 'must-not-exist'
            with patch.dict(os.environ, {'GIT_DIR': str(outside)}):
                init_fixture_repo(repo, root)
            self.assertTrue((repo / '.git/HEAD').is_file())
            self.assertFalse(outside.exists())

    def test_fixture_bank_ids(self):
        self.assertIsNotNone(NODE)
        self.assertTrue(MCP.is_file())
        rebuilt = HERMES_SCRATCH / 'hindsight-coding-087-sparse/hindsight-integrations/coding-agents/dist/mcp-server.js'
        self.assertTrue(rebuilt.is_file(), 'the pinned upstream MCP has not been built')
        entries = (MCP, rebuilt)
        with tempfile.TemporaryDirectory(prefix='coding-route-', dir=FIXTURES) as tmp:
            root = Path(tmp)
            repo, plain = root / 'Probe.v1', root / 'plain-folder'
            repo.mkdir()
            plain.mkdir()
            init_fixture_repo(repo, root)
            nested = repo / 'nested'
            nested.mkdir()
            config = root / 'coding-agent.json'
            config.write_text(json.dumps({
                'serverMode': 'self-hosted', 'apiUrl': ORACLE_URL,
                'dynamicBankId': True, 'bankIdTemplate': 'coding-agent::{gitProject}',
                'autoSeed': False, 'codebaseSurvey': False, 'retainSessions': False,
            }), encoding='utf-8')
            for cwd, expected in (
                (repo, 'coding-agent::Probe.v1'),
                (nested, 'coding-agent::Probe.v1'),
                (plain, 'coding-agent::plain-folder'),
            ):
                for entry in entries:
                    with self.subTest(cwd=cwd.name, entry=str(entry)):
                        self.assertEqual(asyncio.run(diagnose(cwd, config, entry)), expected)
