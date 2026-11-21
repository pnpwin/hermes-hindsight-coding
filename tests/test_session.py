import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from test_settings import ROOT, SCRATCH


def session_module():
    name = '_coding_hermes_session_fixture'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'session.py')
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'coding'
        self.home.mkdir(mode=0o700)

    def make(self, bank='coding-agent::project', sid='../../never-a-path'):
        return session_module().Binding(session_id=sid, start_cwd='/fixture/project', bank_id=bank,
            api_url='http://127.0.0.1:1', config_identity='a' * 64, project_root='/fixture/project',
            project_identity='1:2', write_eligible=True)

    def test_immutable_private_binding_and_no_path_from_session_id(self):
        mod = session_module()
        store = mod.SessionStore(self.home)
        b = self.make()
        self.assertIsNone(store.load(b.session_id))
        self.assertEqual(store.bind(b), b)
        self.assertEqual(mod.SessionStore(self.home).load(b.session_id), b)
        record = next(store.directory.glob('*.json'))
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertRegex(record.name, r'^[0-9a-f]{64}\.json$')
        with self.assertRaisesRegex(mod.SessionError, 'BINDING_CONFLICT'):
            store.bind(self.make(bank='another'))
        self.assertEqual(store.load(b.session_id), b)

    def test_two_profiles_do_not_share_state(self):
        mod = session_module()
        another = self.base / 'other'
        another.mkdir(mode=0o700)
        first, second = mod.SessionStore(self.home), mod.SessionStore(another)
        first.bind(self.make())
        self.assertIsNone(second.load(self.make().session_id))
        second.bind(self.make(bank='other'))
        self.assertNotEqual(first.load(self.make().session_id), second.load(self.make().session_id))

    def test_concurrent_creates_keep_exactly_one_binding(self):
        mod = session_module()
        def bind(bank):
            try:
                return mod.SessionStore(self.home).bind(self.make(bank=bank)).bank_id
            except mod.SessionError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(bind, ['first', 'second']))
        self.assertEqual(results.count('BINDING_CONFLICT'), 1)
        self.assertIn(mod.SessionStore(self.home).load(self.make().session_id).bank_id, ('first', 'second'))

    def test_rejects_symlinked_or_modified_state_instead_of_rebinding(self):
        mod = session_module()
        store = mod.SessionStore(self.home)
        b = self.make()
        store.bind(b)
        record = next(store.directory.glob('*.json'))
        external = self.base / 'elsewhere'
        external.write_text('{}')
        record.unlink()
        record.symlink_to(external)
        with self.assertRaises(mod.SessionError):
            store.load(b.session_id)
        record.unlink()
        record.write_text('{}')
        os.chmod(record, 0o600)
        with self.assertRaises(mod.SessionError):
            store.bind(b)
