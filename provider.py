"""Hermes memory-provider entry point for the Coding Agents engine."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from time import monotonic, time

from agent.memory_provider import MemoryProvider

from .bridge_client import BridgeClient, BridgeError
from .prefetch import context_parts, route_descriptor
from .retain import SessionRetention
from .seed import queue_seed
from .session import Binding, SessionError, SessionStore, WorkLease
from .settings import NAME, Settings, allowed_directory, load_settings
from .tools import READ_TOOLS, bound_session_id, read_schemas

BRIDGE = Path(__file__).resolve().parent / 'bridge/dist/hermes-bridge.mjs'
LOCAL_SURFACES = ('cli', 'tui', 'desktop')
PLUGIN_HOST_MARKER = 'HERMES_PLUGIN_HOST_PROCESS'


def _runtime_node(profile_home: Path) -> Path:
    return profile_home / 'plugin-data' / NAME / 'runtime/node/bin/node'


def _local_execution() -> bool:
    # Hermes 0.23.3 does not forward the caller's TerminalPolicy in memory RPC.
    # A plugin-host environment or profile defaults cannot attest a session's backend.
    if os.environ.get(PLUGIN_HOST_MARKER) == '1':
        return False
    try:
        from tools.terminal_scope import get_terminal_scope, terminal_env

        return get_terminal_scope() is not None and terminal_env('TERMINAL_ENV') == 'local'
    except Exception:
        return False


def _scoped_cwd() -> str:
    try:
        from agent.runtime_cwd import scoped_session_cwd

        return scoped_session_cwd()
    except Exception:
        return ''


class CodingAgentsMemoryProvider(MemoryProvider):
    @property
    def name(self):
        return NAME

    def __init__(self):
        self.session_id = ''
        self.session_cwd: Path | None = None
        self.settings: Settings | None = None
        self.binding: Binding | None = None
        self.inactive_reason = 'not_initialized'
        self._store: SessionStore | None = None
        self._bridge: BridgeClient | None = None
        self._profile_home: Path | None = None
        self._initialized = False
        self._lock = RLock()
        self._lease_owner = object()
        self._generation = 0
        self._fresh_writes = False
        self._replace_pending = False
        self._rewind_at: float | None = None
        self._retention = SessionRetention()
        self.retain_status = 'not_started'

    def is_available(self):
        return (os.environ.get(PLUGIN_HOST_MARKER) != '1'
                and BRIDGE.is_file() and BRIDGE.with_suffix('.sha256').is_file())

    def unavailable_reason(self):
        if os.environ.get(PLUGIN_HOST_MARKER) == '1':
            return 'This Hermes plugin host does not forward the session terminal policy; memory stays inactive.'
        return 'The pinned Coding Agents bridge and a Node executable are required.'

    def initialize(self, session_id, **kwargs):
        with self._lock:
            self._initialize(session_id, **kwargs)

    def _initialize(self, session_id, **kwargs):
        if self._initialized:
            return
        self._initialized = True
        if not isinstance(session_id, str) or not session_id.strip():
            return
        self.session_id = session_id
        self.inactive_reason = 'scope_refused'
        identity = kwargs.get('agent_identity')
        if (not isinstance(identity, str) or not identity.strip() or kwargs.get('agent_context') != 'primary'
                or kwargs.get('platform') not in LOCAL_SURFACES or kwargs.get('parent_session_id')
                or os.environ.get('HERMES_KANBAN_TASK') or not _local_execution()):
            return
        settings = load_settings(kwargs.get('hermes_home'))
        if settings is None:
            return
        explicit = kwargs.get('cwd')
        cwd = allowed_directory(settings, _scoped_cwd() if explicit is None else explicit)
        if cwd is None:
            return
        self.session_cwd = cwd
        self.settings = settings
        if not self.is_available():
            self.inactive_reason = 'bridge_unavailable'
            return
        try:
            profile = Path(kwargs['hermes_home']).resolve(strict=True)
            self._profile_home = profile
            # Bundle manifest belongs to the installed plugin rather than to a workspace.
            with BRIDGE.with_suffix('.sha256').open('r', encoding='ascii') as manifest:
                digest = manifest.read(80).strip()
            self._bridge = BridgeClient(node_path=_runtime_node(profile), bridge_path=BRIDGE,
                bridge_sha256=digest, profile_home=profile, config_path=settings.config_path,
                allowed_roots=settings.allowed_roots, allowed_api_url=settings.allowed_api_url,
                routing_home=Path.home().resolve())
            self._store = SessionStore(profile)
            # Desktop can store the first submitted user prompt before constructing the agent.
            count = kwargs.get('message_count')
            fresh = kwargs.get('resumed') is False and type(count) is int and count in (0, 1)
            self._fresh_writes = self._select_binding(session_id, cwd, allow_create=fresh, write_eligible=fresh)
        except (BridgeError, SessionError, OSError, ValueError):
            self.inactive_reason = 'binding_unavailable'

    def _select_binding(self, session_id, cwd: Path, *, allow_create: bool, write_eligible: bool) -> bool:
        self.binding = None
        store, bridge = self._store, self._bridge
        assert store is not None and bridge is not None
        saved = store.load(session_id)
        if saved is None and not allow_create:
            self.inactive_reason = 'missing_saved_binding'
            return False
        start = Path(saved.start_cwd) if saved else cwd
        route = bridge.resolve(cwd, start)
        if route is None or (saved is not None and not saved.matches_route(route)):
            self.inactive_reason = 'route_mismatch'
            return False
        candidate = saved or Binding.from_route(session_id, route, write_eligible=write_eligible)
        self.binding, created = store.install(candidate)
        self.inactive_reason = ''
        return created and write_eligible

    def _validate_child(self, prior: Binding, session_id: str, cwd: Path) -> None:
        assert self._bridge is not None and self._store is not None
        route = self._bridge.resolve(cwd, Path(prior.start_cwd))
        if route is None or not prior.matches_route(route):
            self.inactive_reason = 'route_mismatch'
            return
        candidate = Binding.from_route(session_id, route, write_eligible=False)
        self.binding = self._store.bind(candidate)
        self.inactive_reason = ''

    def on_session_switch(self, new_session_id, *, parent_session_id='', reset=False, rewound=False, **kwargs):
        with self._lock:
            prior = self.binding
            reason = kwargs.get('reason')
            if reason in ('new_session', 'branch', 'compression'):
                # /new is queued by the host and can arrive after an inline /resume. Its parent
                # must still own the active binding. Resumed parents cannot attest a fresh reset
                # (including a leave-and-resume of the same session id before the callback).
                if prior is None or parent_session_id != self.session_id or new_session_id == self.session_id:
                    return
                if reason == 'new_session' and (not reset or not self._fresh_writes):
                    return
            self._generation += 1
            if rewound and prior is not None and new_session_id == self.session_id:
                # Undo never retargets the write. Only a complete replacement can clear this flag.
                self._replace_pending = True
                self._rewind_at = time()
                return
            self._replace_pending = False
            self._rewind_at = None
            self._retention = SessionRetention()
            self.retain_status = 'not_started'
            self._fresh_writes = False
            self.binding = None
            self.session_cwd = None
            self.session_id = new_session_id
            self.inactive_reason = 'workspace_unconfirmed'
            if (rewound or self.settings is None or self._bridge is None or self._store is None
                    or not _local_execution() or not self._policy_unchanged()):
                return
            live = _scoped_cwd()
            cwd = allowed_directory(self.settings, kwargs.get('cwd') or live)
            if cwd is None or (live and allowed_directory(self.settings, live) != cwd):
                return
            self.session_cwd = cwd
            try:
                if reason in ('resume', 'sessions'):
                    self._select_binding(new_session_id, cwd, allow_create=False, write_eligible=False)
                elif reason in ('branch', 'compression') and prior is not None and parent_session_id == prior.session_id:
                    self._validate_child(prior, new_session_id, cwd)
                elif reason == 'new_session' and reset and (prior is None or new_session_id != prior.session_id):
                    self._fresh_writes = self._select_binding(new_session_id, cwd, allow_create=True, write_eligible=True)
                else:
                    self.inactive_reason = 'unproven_session_lineage'
            except (BridgeError, SessionError, OSError, ValueError):
                self.inactive_reason = 'binding_unavailable'

    @contextmanager
    def _work_lock(self, deadline):
        locked = self._lock.acquire() if deadline is None else self._lock.acquire(timeout=max(0.0, deadline - monotonic()))
        try:
            yield locked
        finally:
            if locked:
                self._lock.release()

    def acquire_work(self, session_id: str, *, writing: bool = False, deadline=None) -> WorkLease | None:
        """Capture the only bank permitted for this event; never re-read a mutable bank after queueing."""
        with self._work_lock(deadline) as locked:
            if not locked:
                return None
            binding, cwd, settings = self.binding, self.session_cwd, self.settings
            if (binding is None or cwd is None or settings is None or self._bridge is None
                    or session_id != self.session_id or not _local_execution() or not self._policy_unchanged()):
                return None
            if writing and not (settings.writes and binding.write_eligible and binding.retain_sessions and self._fresh_writes):
                return None
            live = _scoped_cwd()
            cwd = allowed_directory(settings, live or cwd)
            if cwd is None or not self._route_current(binding, cwd, deadline=deadline):
                return None
            return WorkLease(self._lease_owner, self._generation, binding, cwd, writing,
                             writing and self._replace_pending)

    def prefetch(self, query: str, *, session_id: str = '') -> str:
        ambient_id = bound_session_id()
        if (ambient_id is not None and ambient_id != session_id) or not isinstance(query, str) or not query.strip():
            return ''
        deadline = monotonic() + 5.8
        with self._work_lock(deadline) as locked:
            if not locked:
                return ''
            lease = self.acquire_work(session_id, deadline=deadline)
            bridge = self._bridge
            # A restored or inherited transcript may already carry the original api_content.
            auto_inject = self._fresh_writes
            create = auto_inject and self.settings is not None and self.settings.create_missing_banks
        remaining = deadline - monotonic()
        if lease is None or bridge is None or remaining <= 0:
            return ''
        def grant(send):
            # Publish the grant under the same lock as a session switch/revocation.
            with self._work_lock(deadline) as locked:
                if (not locked or monotonic() >= deadline or bound_session_id() != ambient_id or
                        not self._is_current(lease) or not self._fresh_writes):
                    return False
                send()
                return True
        try:
            result = bridge.prefetch(route_descriptor(lease), session_id=session_id, query=query,
                                     auto_inject=auto_inject, timeout=min(5, remaining), grant_lease=grant if create else None)
        except BridgeError:
            return ''
        return context_parts(result) if bound_session_id() == ambient_id and self.is_current(lease, deadline=deadline) else ''

    def queue_prefetch(self, query, *, session_id=''):
        # This hook runs on Hermes's tracked manager worker, not on the foreground prompt.
        # The query is deliberately unused: only explicitly opted-in commit messages can be seeded.
        queue_seed(self, session_id)

    def sync_turn(self, user_content, assistant_content, *, session_id='', messages=None, turn_author=None):
        ambient = bound_session_id()
        if ((ambient is not None and ambient != session_id) or
                (turn_author is not None and (not isinstance(turn_author, dict) or turn_author.get('is_bot')))):
            return
        deadline = monotonic() + 19.5
        with self._work_lock(deadline) as locked:
            if not locked:
                return
            lease = self.acquire_work(session_id, writing=True, deadline=deadline)
            bridge, retention, rewind_at = self._bridge, self._retention, self._rewind_at
        if lease is None or bridge is None:
            return
        status = retention.submit(self, bridge, lease, messages, user_content, assistant_content, rewind_at=rewind_at,
                                  deadline=deadline, remaining=lambda: deadline - monotonic())
        with self._work_lock(deadline) as locked:
            if locked and lease.generation == self._generation:
                self.retain_status = status

    def _policy_unchanged(self) -> bool:
        return self._profile_home is not None and load_settings(self._profile_home) == self.settings

    def _route_current(self, binding: Binding, cwd: Path, *, deadline=None) -> bool:
        remaining = 5 if deadline is None else min(5, deadline - monotonic())
        if self._bridge is None or remaining <= 0:
            return False
        try:
            route = self._bridge.resolve(cwd, Path(binding.start_cwd), timeout=remaining)
            return route is not None and binding.matches_route(route)
        except (BridgeError, OSError, ValueError):
            return False

    def _is_current(self, lease: WorkLease) -> bool:
        settings = self.settings
        if (settings is None or lease.owner is not self._lease_owner or lease.generation != self._generation
                or self.binding != lease.binding or self.session_id != lease.binding.session_id
                or not _local_execution() or not self._policy_unchanged()):
            return False
        if lease.writing and not (settings.writes and lease.binding.write_eligible and lease.binding.retain_sessions and self._fresh_writes):
            return False
        return allowed_directory(settings, _scoped_cwd() or lease.cwd) == lease.cwd

    def is_current(self, lease: WorkLease, *, deadline=None) -> bool:
        """Check again before dispatch and before publishing a result to the current session."""
        with self._work_lock(deadline) as locked:
            return locked and self._is_current(lease) and self._route_current(lease.binding, lease.cwd, deadline=deadline)

    def acknowledge_replacement(self, lease: WorkLease) -> None:
        with self._lock:
            if lease.writing and lease.replace_snapshot and self.is_current(lease):
                self._replace_pending = False
                self._rewind_at = None

    def shutdown(self):
        with self._lock:
            self._initialized = True
            self._generation += 1
            self.binding = None
            self._bridge = None
            self._store = None
            self._retention = SessionRetention()
            self._rewind_at = None
            self.inactive_reason = 'shutdown'

    def get_tool_schemas(self):
        return read_schemas(BRIDGE.with_suffix('.schemas.json')) if self.is_available() else []

    def handle_tool_call(self, tool_name, args, **kwargs):
        if not isinstance(tool_name, str) or tool_name not in READ_TOOLS or not isinstance(args, dict):
            return '{"error":"TOOL_NOT_ALLOWED"}'
        session_id = bound_session_id()
        if not session_id or (kwargs.get('hermes_home') and Path(kwargs['hermes_home']) != self._profile_home):
            return '{"error":"TOOL_SCOPE_UNCONFIRMED"}'
        lease = self.acquire_work(session_id)
        bridge = self._bridge
        if lease is None or bridge is None:
            return '{"error":"TOOL_SCOPE_UNCONFIRMED"}'
        try:
            result = bridge.call_tool(route_descriptor(lease), tool_name, args)
        except (TypeError, ValueError, BridgeError):
            return '{"error":"HINDSIGHT_READ_FAILED"}'
        if bound_session_id() != session_id or not self.is_current(lease):
            return '{"error":"TOOL_SCOPE_CHANGED"}'
        return json.dumps(result, ensure_ascii=True, allow_nan=False)
