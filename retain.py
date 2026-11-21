"""Prepare retain snapshots in the manager's serialized worker; no independent writer."""

from threading import Lock
from time import time

from .bridge_client import BridgeError
from .prefetch import route_descriptor
from .transcript import TranscriptError, normalize


class SessionRetention:
    def __init__(self):
        self.anchor = time()
        self.lock = Lock()
        self.previous = None
        self.last_rewind = None
        self.rewind_base = None

    def submit(self, provider, bridge, lease, messages, user, reply, *, rewind_at, deadline, remaining):
        if not self.lock.acquire(blocking=False):
            return 'busy'
        try:
            if lease.replace_snapshot and rewind_at is None:
                return 'rewind_unconfirmed'
            if lease.replace_snapshot and self.last_rewind != rewind_at:
                self.last_rewind, self.rewind_base = rewind_at, self.previous
            prepared = normalize(messages, user, reply, anchor=self.anchor,
                previous=self.rewind_base if lease.replace_snapshot else self.previous,
                rewound=lease.replace_snapshot, rewind_after=rewind_at if lease.replace_snapshot else None)
            if not provider.is_current(lease, deadline=deadline) or remaining() <= 0:
                return 'stale'
            self.previous = prepared
            bridge.retain(route_descriptor(lease), session_id=lease.binding.session_id,
                turns=prepared, rewound=lease.replace_snapshot, timeout=remaining())
            if lease.replace_snapshot:
                provider.acknowledge_replacement(lease)
            return 'accepted'
        except TranscriptError:
            return 'unproven_transcript'
        except (BridgeError, ValueError, OSError):
            return 'retain_unavailable'
        finally:
            self.lock.release()
