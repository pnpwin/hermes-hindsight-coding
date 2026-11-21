"""Load local operational policy; Coding Agents settings are resolved separately."""

from dataclasses import dataclass
from ipaddress import ip_address
import json
from pathlib import Path
from urllib.parse import urlsplit

NAME = 'hermes-hindsight-coding'
SETTING_FILE = Path('plugin-data') / NAME / 'config.json'
MAX_CONFIG_BYTES = 64 * 1024
FLAGS = ('writes', 'seed', 'survey', 'manage_bank_config')
KEYS = frozenset(('config_path', 'allowed_roots', 'allowed_api_url', 'seed_bank_ids', 'create_missing_banks',
                  'allow_bank_default_strategy', *FLAGS))


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    allowed_roots: tuple[Path, ...]
    allowed_api_url: str
    writes: bool = False
    seed: bool = False
    survey: bool = False
    manage_bank_config: bool = False
    seed_bank_ids: tuple[str, ...] = ()
    create_missing_banks: bool = False
    allow_bank_default_strategy: bool = False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate configuration key')
        result[key] = value
    return result


def _absolute_existing(value):
    if not isinstance(value, (str, Path)) or not value:
        raise ValueError('path missing')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('path is not absolute')
    return path.resolve(strict=True)


def _loopback_endpoint(value):
    if not isinstance(value, str):
        raise ValueError('endpoint missing')
    parsed = urlsplit(value)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError('host and port required')
    host = ip_address(parsed.hostname)
    authority = f'[{host}]' if host.version == 6 else str(host)
    if not host.is_loopback or not parsed.port or value != f'http://{authority}:{parsed.port}':
        raise ValueError('an exact HTTP loopback origin is required')
    return value


def load_settings(hermes_home) -> Settings | None:
    """Use only the explicit profile; missing or unsafe policy disables the integration."""
    try:
        home = _absolute_existing(hermes_home)
        path = home / SETTING_FILE
        if path.resolve(strict=True) != path or not path.is_file():
            return None
        with path.open('rb') as config:
            raw = config.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) - KEYS:
            return None
        upstream = _absolute_existing(data['config_path'])
        if not upstream.is_file():
            return None
        entries = data['allowed_roots']
        if not isinstance(entries, list) or not entries:
            return None
        resolved = tuple(map(_absolute_existing, entries))
        user_home = Path.home().resolve()
        if any(not root.is_dir() or (root.samefile(user_home) and raw != str(user_home)) or root.samefile(root.anchor)
               for raw, root in zip(entries, resolved, strict=True)):
            return None
        roots = tuple(dict.fromkeys(resolved))
        flags = {flag: data.get(flag, False) for flag in FLAGS}
        create = data.get('create_missing_banks', False)
        fallback = data.get('allow_bank_default_strategy', False)
        if type(create) is not bool or type(fallback) is not bool or any(type(value) is not bool for value in flags.values()):
            return None
        if not flags['writes'] and any(flags[flag] for flag in FLAGS[1:]):
            return None
        seed_banks = data.get('seed_bank_ids', [])
        if (not isinstance(seed_banks, list) or len(seed_banks) > 32 or
                any(not isinstance(bank, str) or not bank.strip() or bank in ('.', '..') or len(bank) > 512 or
                    any(ord(ch) < 32 for ch in bank) for bank in seed_banks) or
                bool(seed_banks) != flags['seed'] or len(set(seed_banks)) != len(seed_banks)):
            return None
        return Settings(upstream, roots, _loopback_endpoint(data['allowed_api_url']), seed_bank_ids=tuple(seed_banks),
                        create_missing_banks=create, allow_bank_default_strategy=fallback, **flags)
    except (OSError, ValueError, KeyError, RuntimeError):
        return None


def allowed_directory(settings: Settings, value) -> Path | None:
    """Check the physical directory, not a lexical path or string prefix."""
    try:
        directory = _absolute_existing(value)
        if not directory.is_dir():
            return None
        home = Path.home().resolve(strict=True)
        if directory.samefile(home):
            return home if home in settings.allowed_roots else None
        if any(root != home and directory.is_relative_to(root) for root in settings.allowed_roots):
            return directory
    except (OSError, ValueError, RuntimeError):
        pass
    return None
