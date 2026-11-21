"""Read-only memory-tool schemas generated from the pinned upstream page tool specs."""

import json

READ_TOOLS = frozenset(('hindsight_diagnose', 'hindsight_sync_status', 'hindsight_list_pages',
                        'hindsight_search_pages', 'hindsight_read_page', 'hindsight_reflect'))


def read_schemas(path):
    try:
        if path.is_symlink():
            return []
        with path.open('rb') as file:
            raw = file.read(32769)
        if len(raw) > 32768:
            return []
        schemas = json.loads(raw)
        if not isinstance(schemas, list) or len(schemas) != len(READ_TOOLS):
            return []
        functions = [entry['function'] for entry in schemas if entry['type'] == 'function']
        if ({f['name'] for f in functions} != READ_TOOLS or any(
                f['parameters']['type'] != 'object' or f['parameters'].get('additionalProperties') is not False
                for f in functions)):
            return []
        return schemas
    except (OSError, KeyError, ValueError, TypeError, AttributeError, RecursionError):
        return []


def bound_session_id() -> str | None:
    # Pinned Hermes exposes get_session_env(), but that falls back to process-global env.
    # This adapter needs the task-local value only, including an explicitly empty value.
    try:
        from gateway.session_context import _SESSION_ID

        value = _SESSION_ID.get(None)
        return value if isinstance(value, str) else None
    except (ImportError, AttributeError):
        return ''  # unsupported host must not widen scope
