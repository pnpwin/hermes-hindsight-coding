"""Profile-local, immutable session bindings; no transcripts or credentials."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

NAME = 'hermes-hindsight-coding'
MAX_RECORD = 32 * 1024


class SessionError(RuntimeError):
    """Stable failure code; never includes content from a corrupted binding."""


@dataclass(frozen=True, slots=True)
class Binding:
    session_id: str
    start_cwd: str
    bank_id: str
    api_url: str
    config_identity: str
    project_root: str | None
    project_identity: str | None
    write_eligible: bool
    retain_sessions: bool = False
    seed_message: bool = False

    @classmethod
    def from_route(cls, session_id, route, *, write_eligible):
        return cls(session_id, route['startCwd'], route['bankId'], route['apiUrl'], route['configIdentity'],
                   route['projectRoot'], route['projectIdentity'], write_eligible, route.get('retainSessions') is True,
                   route.get('seedMessage') is True)

    def matches_route(self, route):
        return self == self.from_route(self.session_id, route, write_eligible=self.write_eligible)


@dataclass(frozen=True, slots=True)
class WorkLease:
    owner: object
    generation: int
    binding: Binding
    cwd: Path
    writing: bool = False
    replace_snapshot: bool = False


def _name(session_id):
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 1024:
        raise SessionError('INVALID_SESSION')
    try:
        return hashlib.sha256(session_id.encode('utf-8')).hexdigest() + '.json'
    except UnicodeError:
        raise SessionError('INVALID_SESSION') from None


def _valid(data: Binding):
    text = (data.session_id, data.start_cwd, data.bank_id, data.api_url, data.config_identity)
    return (all(isinstance(value, str) and value and len(value) <= 4096 and not any(ord(c) < 32 for c in value)
                for value in text)
            and Path(data.start_cwd).is_absolute()
            and len(data.bank_id) <= 512 and data.bank_id.strip() and data.bank_id not in ('.', '..')
            and re.fullmatch(r'http://(?:127(?:\.\d{1,3}){3}|\[::1\]):[1-9]\d{0,4}', data.api_url)
            and re.fullmatch(r'[a-f0-9]{64}', data.config_identity)
            and ((data.project_root is None and data.project_identity is None) or
                 (isinstance(data.project_root, str) and Path(data.project_root).is_absolute() and
                  isinstance(data.project_identity, str) and re.fullmatch(r'\d{1,24}:\d{1,24}', data.project_identity)))
            and type(data.write_eligible) is bool and type(data.retain_sessions) is bool and type(data.seed_message) is bool)


def _no_duplicates(pairs):
    result = {}
    for k, value in pairs:
        if k in result:
            raise ValueError
        result[k] = value
    return result


class SessionStore:
    def __init__(self, profile_home: Path):
        self.home = Path(profile_home)
        self.directory = self.home / 'plugin-data' / NAME / 'sessions-v1'
        with self._directory_fd() as fd:
            st = os.fstat(fd)
            self._identity = (st.st_dev, st.st_ino)

    @contextmanager
    def _directory_fd(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fds = []
        try:
            if not self.home.is_absolute() or self.home.resolve(strict=True) != self.home:
                raise ValueError
            fds.append(os.open(self.home, flags))
            expected = getattr(self, '_identity', None)
            for part in ('plugin-data', NAME, 'sessions-v1'):
                if expected is None:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fds[-1])
                    except FileExistsError:
                        pass
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
                st = os.fstat(fds[-1])
                if st.st_uid != os.geteuid() or st.st_mode & 0o022:
                    raise ValueError
            if st.st_mode & 0o077:
                raise ValueError
            if expected is not None and (st.st_dev, st.st_ino) != expected:
                raise ValueError
            yield fds[-1]
        except (ValueError, OSError):
            raise SessionError('UNSAFE_SESSION_STORE') from None
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def _read(self, session_id, key, directory):
        try:
            fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError:
            raise SessionError('INVALID_BINDING') from None
        with os.fdopen(fd, 'rb') as record:
            st = os.fstat(record.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise SessionError('INVALID_BINDING')
            raw = record.read(MAX_RECORD + 1)
        try:
            if len(raw) > MAX_RECORD:
                raise ValueError
            fields = json.loads(raw, object_pairs_hook=_no_duplicates)
            binding = Binding(**fields)
            if binding.session_id != session_id or not _valid(binding):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise SessionError('INVALID_BINDING') from None
        return binding

    def load(self, session_id):
        key = _name(session_id)
        with self._directory_fd() as directory:
            return self._read(session_id, key, directory)

    def bind(self, binding: Binding):
        return self.install(binding)[0]

    def install(self, binding: Binding) -> tuple[Binding, bool]:
        """Publish a complete record by atomic no-replace link; the first process wins."""
        key = _name(binding.session_id)
        if not _valid(binding):
            raise SessionError('INVALID_BINDING')
        body = json.dumps(asdict(binding), separators=(',', ':'), ensure_ascii=True).encode('ascii')
        if len(body) > MAX_RECORD:
            raise SessionError('INVALID_BINDING')
        with self._directory_fd() as directory:
            current = self._read(binding.session_id, key, directory)
            created = False
            if current is None:
                temp = '.' + secrets.token_hex(16)
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temp, key, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                        os.fsync(directory)
                        created = True
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(temp, dir_fd=directory)
                current = self._read(binding.session_id, key, directory)
            if current != binding:
                raise SessionError('BINDING_CONFLICT')
            return current, created
