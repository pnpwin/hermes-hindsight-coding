"""Opt-in Git message seed from Hermes's own tracked post-turn worker."""

import re
from time import monotonic

from agent.redact import redact_for_egress

from .bridge_client import BridgeError
from .prefetch import route_descriptor
from .tools import bound_session_id

_DATA_MARKER = re.compile(r'(?:^|[^a-z0-9_])data:', re.IGNORECASE | re.ASCII)


def clean_seed_text(text):
    # Fail closed rather than silently changing the upstream snapshot and its operation identity.
    return isinstance(text, str) and bool(text) and not _DATA_MARKER.search(text) and redact_for_egress(text) == text


def queue_seed(provider, session_id):
    ambient = bound_session_id()
    if ambient is not None and ambient != session_id:
        return
    deadline = monotonic() + 17
    with provider._work_lock(deadline) as locked:
        settings, binding = provider.settings, provider.binding
        if (not locked or settings is None or binding is None or not settings.writes or not settings.seed
                or binding.bank_id not in settings.seed_bank_ids or not binding.project_root or not binding.seed_message
                or not provider._fresh_writes):
            return
        lease = provider.acquire_work(session_id, deadline=deadline)
        bridge = provider._bridge
    if lease is None or bridge is None:
        return

    def still_current():
        return bound_session_id() == ambient and provider.is_current(lease, deadline=deadline)

    def grant_lease(send):
        # The socket write is the handoff: /new, /resume and shutdown use this same lock.
        with provider._work_lock(deadline) as locked:
            if not locked or not still_current() or not provider._is_current(lease) or monotonic() >= deadline:
                return False
            send()
            return True

    try:
        bridge.seed(route_descriptor(lease), deadline=deadline, validate_text=clean_seed_text,
                    still_current=still_current, grant_lease=grant_lease)
    except (BridgeError, ValueError, OSError):
        return
