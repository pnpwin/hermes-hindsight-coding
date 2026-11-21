"""Bounded, profile-local transport to the pinned Coding Agents engine."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
from time import monotonic

from .settings import NAME, load_settings

MAX_WIRE = 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_BUNDLE = 4 * 1024 * 1024
NODE_PIN = Path(__file__).resolve().parent / 'engine/NODE.json'
SAFE_PATH = '/usr/bin:/bin'
ERRORS = frozenset(('INVALID_REQUEST', 'CONFIG_INVALID', 'ROUTE_REJECTED', 'WIRE_LIMIT', 'RUNTIME_UNSUPPORTED', 'PREFETCH_FAILED', 'RETAIN_FAILED', 'SEED_FAILED'))
_LOAD_VERIFIED = r'''
import { statSync, realpathSync } from 'node:fs';
const fail = (code) => ({v:1, ok:false, error:{code}});
const workerMs = Number(process.env.HERMES_WORKER_DEADLINE_MS);
if (Number.isFinite(workerMs) && workerMs > 0) setTimeout(() => process.exit(124), workerMs).unref();
async function run() {
  if (process.versions.node !== '22.22.3') return fail('RUNTIME_UNSUPPORTED');
  const st = statSync('.', {bigint:true});
  if (`${st.dev}:${st.ino}` !== process.env.HERMES_WORKER_ID ||
      realpathSync(process.env.HOME) !== process.env.HOME ||
      realpathSync('.') !== process.env.HOME) return fail('INVALID_REQUEST');
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 5 * 1024 * 1024 + 4) return fail('WIRE_LIMIT');
    chunks.push(chunk);
  }
  const data = Buffer.concat(chunks, size);
  const n = data.readUInt32BE();
  if (!n || n > 4 * 1024 * 1024 || size <= n + 4 || size - n - 4 > 1024 * 1024) return fail('INVALID_REQUEST');
  const bridge = await import('data:text/javascript;base64,' + data.subarray(4, n+4).toString('base64'));
  return await bridge.handleWire(data.subarray(n+4), process.env.HINDSIGHT_CONFIG);
}
try {
  const output = JSON.stringify(await run());
  process.stdout.write((Buffer.byteLength(output) < 1024 * 1024 ? output : JSON.stringify(fail('WIRE_LIMIT'))) + '\n');
} catch { process.stdout.write(JSON.stringify(fail('INVALID_REQUEST')) + '\n'); }
'''


class BridgeError(RuntimeError):
    """A stable error code without transcript, config or subprocess output."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _verified_node(node_path):
    try:
        path = Path(node_path)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
            raise ValueError
        pin = json.loads(NODE_PIN.read_text(encoding='utf-8'))
        if f'{os.uname().sysname.lower()}-{os.uname().machine}' != pin['platform']:
            raise ValueError
        with path.open('rb') as the_file:
            digest = hashlib.file_digest(the_file, 'sha256').hexdigest()
        if digest != pin['nodeSHA256']:
            raise ValueError
        return path, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise BridgeError('NODE_PIN_MISMATCH') from None


def _worker_dir(profile_home):
    try:
        profile = Path(profile_home)
        if not profile.is_absolute() or not profile.is_dir():
            raise ValueError
        current = profile.resolve(strict=True)
        for name in ('plugin-data', NAME, 'bridge-worker'):
            current = current / name
            current.mkdir(mode=0o700, exist_ok=True)
            st = current.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
                raise ValueError
        if st.st_mode & 0o077:
            raise ValueError
        return current
    except (OSError, ValueError, TypeError, RuntimeError):
        raise BridgeError('UNSAFE_WORKER_DIR') from None


def _run_bounded(argv, payload, env, cwd, *, timeout, input_limit=MAX_WIRE, pass_fds=(), lease_channel=None):
    if len(payload) > input_limit:
        raise BridgeError('WIRE_LIMIT')
    deadline = monotonic() + timeout
    reply = bytearray()
    sent = err = 0
    try:
        with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True, close_fds=True, umask=0o077,
                              pass_fds=pass_fds) as proc, selectors.DefaultSelector() as selector:
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            try:
                if lease_channel is not None:
                    _, child_end, _ = lease_channel
                    child_end.close()  # Only the worker may keep this peer alive.
                for stream, direction, name in ((proc.stdin, selectors.EVENT_WRITE, 'in'),
                                                (proc.stdout, selectors.EVENT_READ, 'out'),
                                                (proc.stderr, selectors.EVENT_READ, 'err')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, direction, name)
                if lease_channel is not None:
                    channel, _, _ = lease_channel
                    channel.setblocking(False)
                    selector.register(channel, selectors.EVENT_READ, 'lease')
                while selector.get_map():
                    left = deadline - monotonic()
                    if left <= 0:
                        raise BridgeError('DEADLINE')
                    for key, _ in selector.select(left):
                        stream, fd = key.fileobj, key.fd
                        try:
                            if key.data == 'lease':
                                assert lease_channel is not None
                                channel, _, grant = lease_channel
                                command = channel.recv(2)
                                if not command:
                                    selector.unregister(stream)
                                    continue
                                if command != b'?':
                                    raise BridgeError('INVALID_REQUEST')
                                # The owner serializes its final generation check and this send.
                                if not grant(lambda: channel.sendall(b'+')):
                                    channel.sendall(b'-')
                                continue
                            if key.data == 'in':
                                sent += os.write(fd, payload[sent:sent + 65536])
                                if sent == len(payload):
                                    selector.unregister(stream)
                                    proc.stdin.close()
                                continue
                            chunk = os.read(fd, 65536)
                        except BrokenPipeError:
                            selector.unregister(stream)
                            if key.data == 'in':
                                proc.stdin.close()
                            continue
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if key.data == 'out':
                            if len(reply) + len(chunk) > MAX_WIRE:
                                raise BridgeError('WIRE_LIMIT')
                            reply.extend(chunk)
                        else:
                            err += len(chunk)
                            if err > MAX_STDERR:
                                raise BridgeError('WIRE_LIMIT')
                exit_code = proc.wait(timeout=max(0, deadline - monotonic()))
            except BaseException as exc:
                # The group was solely created for this worker, never shared with Hermes or Codex.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise BridgeError('DEADLINE') from None
                raise
            if exit_code != 0:
                raise BridgeError('WORKER_FAILED')
    except OSError:
        raise BridgeError('WORKER_FAILED') from None
    return bytes(reply)


def _directory_identity(path, code):
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError
        return st.st_dev, st.st_ino
    except (OSError, ValueError, RuntimeError):
        raise BridgeError(code) from None


def _bundle_bytes(path, expected):
    try:
        if (not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)
                or not path.is_absolute() or path.resolve(strict=True) != path):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BUNDLE:
                raise ValueError
            content = source.read(MAX_BUNDLE + 1)
        if not content or len(content) > MAX_BUNDLE or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError
        return content
    except (OSError, ValueError, RuntimeError):
        raise BridgeError('BRIDGE_INTEGRITY') from None


class BridgeClient:
    def __init__(self, *, node_path, bridge_path, bridge_sha256, profile_home, config_path, allowed_roots, allowed_api_url,
                 routing_home):
        self.node, self._node_signature = _verified_node(node_path)
        self._bundle = _bundle_bytes(Path(bridge_path), bridge_sha256)
        if not Path(config_path).is_absolute():
            raise BridgeError('CONFIG_INVALID')
        self._profile_home = Path(profile_home)
        self.worker_home = _worker_dir(self._profile_home)
        self._worker_identity = _directory_identity(self.worker_home, 'UNSAFE_WORKER_DIR')
        self.config_path = Path(config_path)
        self.allowed_roots = tuple(str(root) for root in allowed_roots)
        self._root_ids = tuple(_directory_identity(Path(root), 'SCOPE_CHANGED') for root in self.allowed_roots)
        self.allowed_api_url = allowed_api_url
        self.routing_home = str(routing_home)

    def _check_worker(self):
        if (_worker_dir(self._profile_home) != self.worker_home
                or _directory_identity(self.worker_home, 'UNSAFE_WORKER_DIR') != self._worker_identity):
            raise BridgeError('UNSAFE_WORKER_DIR')
        if tuple(_directory_identity(Path(root), 'SCOPE_CHANGED') for root in self.allowed_roots) != self._root_ids:
            raise BridgeError('SCOPE_CHANGED')

    def _request(self, op, args, *, timeout, lock_fd=None, grant_lease=None):
        self._check_worker()
        try:
            st = self.node.stat()
        except OSError:
            raise BridgeError('NODE_PIN_MISMATCH') from None
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != self._node_signature:
            raise BridgeError('NODE_PIN_MISMATCH')
        payload = json.dumps({'v': 1, 'op': op, 'args': args}, separators=(',', ':'), allow_nan=False, ensure_ascii=False).encode('utf-8')
        if len(payload) > MAX_WIRE:
            raise BridgeError('WIRE_LIMIT')
        env = {'PATH': SAFE_PATH, 'HOME': str(self.worker_home), 'TMPDIR': str(self.worker_home),
               'HINDSIGHT_CONFIG': str(self.config_path), 'HINDSIGHT_LOG_FILE': os.devnull,
               'HINDSIGHT_DIAG_FILE': os.devnull, 'HINDSIGHT_LOG_LEVEL': 'error',
               'HERMES_WORKER_ID': ':'.join(map(str, self._worker_identity))}
        if lock_fd is not None:
            env['HERMES_RETAIN_LOCK_FD'] = str(lock_fd)
            env['HERMES_WORKER_DEADLINE_MS'] = str(max(1, int(timeout * 1000)))
        frame = len(self._bundle).to_bytes(4, 'big') + self._bundle + payload
        argv = [str(self.node), '--input-type=module', '-e', _LOAD_VERIFIED]
        kwargs = dict(timeout=timeout, input_limit=MAX_BUNDLE + MAX_WIRE + 4)
        pins = () if lock_fd is None else (lock_fd,)
        if grant_lease is None:
            raw = _run_bounded(argv, frame, env, self.worker_home, pass_fds=pins, **kwargs)
        else:
            parent, child = socket.socketpair()
            with parent, child:
                env['HERMES_WRITE_LEASE_FD'] = str(child.fileno())
                raw = _run_bounded(argv, frame, env, self.worker_home, pass_fds=(*pins, child.fileno()),
                                   lease_channel=(parent, child, grant_lease), **kwargs)
        try:
            obj = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
            if not isinstance(obj, dict) or type(obj.get('v')) is not int or obj['v'] != 1:
                raise ValueError
            if obj.get('ok') is True and set(obj) == {'v', 'ok', 'data'} and isinstance(obj['data'], dict):
                return obj['data']
            if obj.get('ok') is False and set(obj) == {'v', 'ok', 'error'}:
                error = obj['error']
                if isinstance(error, dict) and set(error) == {'code'} and isinstance(error['code'], str) and error['code'] in ERRORS:
                    raise BridgeError(error['code'])
            raise ValueError
        except (ValueError, RecursionError):
            raise BridgeError('INVALID_REPLY') from None

    def resolve(self, cwd: Path, start_cwd: Path, *, timeout: float = 5):
        pins = [{'path': root, 'dev': str(dev), 'ino': str(ino)}
                for root, (dev, ino) in zip(self.allowed_roots, self._root_ids, strict=True)]
        args = {'cwd': str(cwd), 'startCwd': str(start_cwd), 'rootPins': pins,
                'routingHome': self.routing_home, 'allowedApiUrl': self.allowed_api_url}
        data = self._request('resolve', args, timeout=timeout)
        if set(data) == {'active'} and data['active'] is False:
            return None
        fields = {'active', 'bankId', 'apiUrl', 'cwd', 'startCwd', 'projectRoot', 'projectIdentity', 'configIdentity', 'retainSessions', 'seedMessage'}
        if type(data.get('retainSessions')) is not bool or type(data.get('seedMessage')) is not bool:
            raise BridgeError('INVALID_REPLY')
        try:
            if (set(data) != fields or data['active'] is not True or data['apiUrl'] != self.allowed_api_url
                    or not isinstance(data['bankId'], str) or not data['bankId'].strip()
                    or not isinstance(data['configIdentity'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['configIdentity'])
                    or data['cwd'] != str(cwd.resolve(strict=True)) or data['startCwd'] != str(start_cwd.resolve(strict=True))
                    or (data['projectRoot'] is None and data['projectIdentity'] is not None)):
                raise ValueError
            if data['projectRoot'] is not None:
                root = Path(data['projectRoot'])
                if not any(root.is_relative_to(approved) for approved in self.allowed_roots):
                    raise ValueError
                if data['projectIdentity'] != ':'.join(map(str, _directory_identity(root, 'INVALID_REPLY'))):
                    raise ValueError
        except (OSError, ValueError, TypeError):
            raise BridgeError('INVALID_REPLY') from None
        return data

    def _scoped_args(self, binding):
        pins = [{'path': root, 'dev': str(dev), 'ino': str(ino)}
                for root, (dev, ino) in zip(self.allowed_roots, self._root_ids, strict=True)]
        return {'cwd': binding['cwd'], 'startCwd': binding['startCwd'], 'rootPins': pins,
                'routingHome': self.routing_home, 'allowedApiUrl': self.allowed_api_url}

    def call_tool(self, binding, name, arguments):
        data = self._request('tool', {**self._scoped_args(binding), 'binding': binding,
                                      'tool': name, 'input': arguments}, timeout=29)
        if (set(data) != {'provenance', 'result'} or not isinstance(data.get('provenance'), str)) and (
                set(data) != {'error'} or not isinstance(data['error'], str)):
            raise BridgeError('INVALID_REPLY')
        return data

    def retain(self, binding, *, session_id: str, turns, rewound: bool, timeout: float = 19):
        deadline = monotonic() + min(19.0, timeout)
        self._check_worker()
        policy = load_settings(self._profile_home)
        if (policy is None or not policy.writes or policy.config_path != self.config_path
                or policy.allowed_api_url != self.allowed_api_url or tuple(map(str, policy.allowed_roots)) != self.allowed_roots):
            raise BridgeError('RETAIN_DISABLED')

        def digest(parts):
            return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()

        engine_id = 'hermes-' + digest(['hermes', str(self._profile_home), session_id])
        key = digest([binding['bankId'], engine_id])
        directory = self.worker_home / 'retain-cursors'
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            st = directory.lstat()
            if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(st.st_mode)
                    or st.st_uid != os.geteuid() or st.st_mode & 0o077):
                raise OSError('private cursor directory required')
            fd = os.open(directory / (key + '.lock'), os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'rb') as lock:
                pin = os.fstat(fd)
                if not stat.S_ISREG(pin.st_mode) or pin.st_nlink != 1 or pin.st_uid != os.geteuid() or pin.st_mode & 0o077:
                    raise OSError('private cursor lock required')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if binding.get('retainSessions') is not True or self.resolve(
                        Path(binding['cwd']), Path(binding['startCwd']), timeout=min(3.0, deadline - monotonic())) != binding:
                    raise BridgeError('ROUTE_REJECTED')
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise BridgeError('DEADLINE')
                data = self._request('retain', {**self._scoped_args(binding), 'binding': binding, 'engineId': engine_id,
                    'primary': True, 'turns': turns, 'rewound': rewound}, timeout=remaining, lock_fd=lock.fileno())
        except OSError:
            raise BridgeError('RETAIN_BUSY_OR_UNSAFE') from None
        if data != {'accepted': True}:
            raise BridgeError('INVALID_REPLY')

    def seed(self, binding, *, deadline, validate_text, still_current, grant_lease):
        self._check_worker()
        policy = load_settings(self._profile_home)
        if (policy is None or not policy.writes or not policy.seed or binding['bankId'] not in policy.seed_bank_ids
                or binding.get('seedMessage') is not True or not binding.get('projectRoot')):
            return
        doc = 'gitlog:' + Path(binding['projectRoot']).name
        if not all(map(validate_text, (doc, binding['bankId'], f'git commit-message history for {doc[7:]}'))):
            return
        key = hashlib.sha256(json.dumps([binding['bankId'], doc], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        directory = self.worker_home / 'seed-state'
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            st = directory.lstat()
            if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(st.st_mode)
                    or st.st_uid != os.geteuid() or st.st_mode & 0o077):
                raise OSError('private seed state required')
            fd = os.open(directory / (key + '.lock'), os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'rb') as lock:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                    raise OSError('private seed lock required')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                args = {**self._scoped_args(binding), 'binding': binding}
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                preview = self._request('seed', {**args, 'digest': None}, timeout=min(5.5, remaining), lock_fd=lock.fileno())
                digest = preview.get('digest')
                if (preview.get('status') != 'preview' or set(preview) != {'status', 'digest', 'content'}
                        or not isinstance(digest, str) or re.fullmatch(r'[a-f0-9]{64}', digest) is None
                        or not validate_text(preview['content']) or not still_current()):
                    return
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                result = self._request('seed', {**args, 'digest': digest}, timeout=remaining, lock_fd=lock.fileno(),
                                       grant_lease=grant_lease)
                if result not in ({'status': 'accepted'}, {'status': 'current'}, {'status': 'deferred'}, {'status': 'no_history'}):
                    raise BridgeError('INVALID_REPLY')
        except OSError:
            raise BridgeError('SEED_BUSY_OR_UNSAFE') from None

    def _cache_file(self, session_id):
        try:
            cache = self.worker_home / 'prefetch-cache'
            cache.mkdir(mode=0o700, exist_ok=True)
            if cache.resolve(strict=True) != cache or not cache.is_dir() or cache.stat().st_mode & 0o077:
                raise ValueError
        except (OSError, ValueError):
            raise BridgeError('UNSAFE_WORKER_DIR') from None
        return cache / (hashlib.sha256(session_id.encode()).hexdigest() + '.json')

    def prefetch(self, binding, *, session_id, query, auto_inject: bool, timeout: float = 5, grant_lease=None):
        cache = self._cache_file(session_id)
        args = {**self._scoped_args(binding), 'sessionId': session_id, 'query': query,
                'cacheFile': str(cache), 'binding': binding, 'autoInject': auto_inject}
        try:
            fd = os.open(cache.with_suffix('.lock'), os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'rb') as lock:
                st = os.fstat(lock.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                    raise OSError('unsafe lock')
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = self._request('prefetch', args, timeout=timeout, grant_lease=grant_lease)
        except OSError:
            raise BridgeError('CACHE_UNAVAILABLE') from None
        fields = {'systemMessage', 'additionalContext', 'context', 'notice', 'deferInitialReflect'}
        if (set(data) != fields or any(not isinstance(data[key], str) for key in fields - {'deferInitialReflect'})
                or type(data['deferInitialReflect']) is not bool):
            raise BridgeError('INVALID_REPLY')
        return data
