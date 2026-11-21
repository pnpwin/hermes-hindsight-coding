import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path.home() / '.hermes/cache/scratch'


def settings_module():
    import sys
    name = '_coding_hermes_settings_fixture'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'settings.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / 'profile'
        self.path = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
        self.path.parent.mkdir(parents=True)
        self.root = base / 'allowed-project'
        self.root.mkdir()
        self.upstream = base / 'coding-agent.json'
        self.upstream.write_text('{}', encoding='utf-8')
        self.data = {'config_path': str(self.upstream), 'allowed_roots': [str(self.root)],
                     'allowed_api_url': 'http://127.0.0.1:8888'}

    def load(self, **changes):
        self.path.write_text(json.dumps(self.data | changes), encoding='utf-8')
        return settings_module().load_settings(self.home)

    def test_write_controls_default_to_false(self):
        cfg = self.load()
        self.assertEqual(cfg.config_path, self.upstream)
        self.assertEqual(cfg.allowed_roots, (self.root,))
        self.assertEqual(cfg.allowed_api_url, 'http://127.0.0.1:8888')
        self.assertFalse(any((cfg.writes, cfg.seed, cfg.survey, cfg.manage_bank_config)))

    def test_exact_home_opt_in_never_allows_children_by_itself(self):
        operator = self.home.parent/'operator'
        operator.mkdir()
        child = operator/'child'
        child.mkdir()
        with patch.object(Path, 'home', return_value=operator):
            cfg = self.load(allowed_roots=[str(operator)])
            self.assertIsNotNone(cfg)
            self.assertEqual(settings_module().allowed_directory(cfg, operator), operator)
            self.assertIsNone(settings_module().allowed_directory(cfg, child))

    def test_only_explicit_profile_file_is_loaded(self):
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home.parent)}):
            self.assertIsNone(settings_module().load_settings(self.home))
            self.assertIsNotNone(self.load())
            self.assertIsNone(settings_module().load_settings(self.home.parent))

    def test_symlink_cannot_opt_in_to_home(self):
        operator = self.home.parent/'operator'
        operator.mkdir()
        alias = self.home.parent/'alias'
        alias.symlink_to(operator, target_is_directory=True)
        with patch.object(Path, 'home', return_value=operator):
            self.assertIsNone(self.load(allowed_roots=[str(alias)]))

    def test_rejects_unknown_controls_types_and_nonlocal_endpoint(self):
        for changes in (
            {'extra': True}, {'writes': 1}, {'seed': True}, {'allowed_roots': []},
            {'allowed_roots': ['/']},
            {'config_path': 'not-absolute.json'}, {'config_path': str(self.root)},
            {'allowed_api_url': 'https://api.hindsight.vectorize.io'},
            {'allowed_api_url': 'http://localhost:8888'},
            {'allowed_api_url': 'http://user:secret@127.0.0.1:8888'},
            {'allowed_api_url': 'http://127.0.0.1:8888/other'},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(self.load(**changes))

    def test_rejects_duplicate_keys_and_symlinked_profile_config(self):
        raw = json.dumps(self.data)[:-1] + ',"writes":false,"writes":true}'
        self.path.write_text(raw, encoding='utf-8')
        self.assertIsNone(settings_module().load_settings(self.home))
        self.path.unlink()
        external = self.home.parent / 'outside.json'
        external.write_text(json.dumps(self.data), encoding='utf-8')
        self.path.symlink_to(external)
        self.assertIsNone(settings_module().load_settings(self.home))

    def test_rejects_physical_home_via_case_alias(self):
        home = Path.home().resolve()
        alias = home.with_name(home.name.swapcase())
        if alias == home or not alias.is_dir() or not alias.samefile(home):
            self.skipTest('requires a case-insensitive home filesystem')
        self.assertIsNone(self.load(allowed_roots=[str(alias)]))
