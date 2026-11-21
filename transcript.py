"""Project only scrubbed Hermes transcript rows into Coding Agents TransportTurn values."""

from datetime import datetime, timedelta, timezone
import json
import math
import re

from session_rules.turns import COMPRESSION_CONTINUATION_USER_CONTENT, is_compaction_summary_message, withheld_prompt_text

MAX_ROWS = 4096
MAX_TURNS = 4096
MAX_TRANSCRIPT_BYTES = 512 * 1024
_TOOL_NAME = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]{0,127}', re.ASCII)
_FENCE = re.compile(
    r'</?(memory-context|context_management_guidance|hook_prompt|task-notification|system-reminder|'
    r'hindsight_memory|hindsight_memories|hindsight_bank|relevant_memories|user_feedback|'
    r'hindsight_pages|hindsight_page_refresh|hindsight_knowledge|hindsight_knowledge_refresh)\b[^<>]*>',
    re.IGNORECASE,
)
_IMAGE_STAMP = re.compile(r'^\[[1-9][0-9]* images?\](?: |$)')
_DATA_MARKER = re.compile(r'(?:^|[^a-z0-9_])data:', re.IGNORECASE | re.ASCII)
_WITHHELD = '[Message withheld: a pre_llm_call policy hook blocked it.]'
_SKIP_KINDS = frozenset(('bootstrap', 'hidden', 'withheld', 'notice', 'system_notice'))


class TranscriptError(ValueError):
    """Stable failure code, without disclosing any transcript data."""


def _timestamp(value):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str) and len(value) <= 48:
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        pass
    raise TranscriptError('UNPROVEN_TIMESTAMP')


def _iso(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _strip_injected(text):
    if len(text) > MAX_TRANSCRIPT_BYTES:
        raise TranscriptError('TRANSCRIPT_LIMIT')
    # Hermes lets media URI strings bypass scrubbing. Fail closed for these markers in
    # text regardless of whitespace, case, or a preceding part/wrapper.
    if _DATA_MARKER.search(text):
        raise TranscriptError('UNSCRUBBED_DATA_URI')
    out, fences, last = [], [], 0
    for match in _FENCE.finditer(text):
        tag = match[1].lower()
        if not fences:
            out.append(text[last:match.start()])
        if match[0].startswith('</'):
            if not fences or fences.pop() != tag:
                raise TranscriptError('AMBIGUOUS_INJECTION')
        else:
            fences.append(tag)
        last = match.end()
    if fences:
        raise TranscriptError('AMBIGUOUS_INJECTION')
    out.append(text[last:])
    text = ''.join(out).strip()
    # Removing a fence can also join a marker that did not exist in the source string.
    if _DATA_MARKER.search(text):
        raise TranscriptError('UNSCRUBBED_DATA_URI')
    return text


def _text(value):
    if isinstance(value, str):
        return _strip_injected(value)
    if not isinstance(value, list):
        return ''
    if len(value) > MAX_ROWS:
        raise TranscriptError('TRANSCRIPT_LIMIT')
    bits = []
    units = 0
    for part in value:
        if isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text') and isinstance(part.get('text'), str):
            bits.append(part['text'])
            units += len(part['text']) + 1
            if units > MAX_TRANSCRIPT_BYTES:
                raise TranscriptError('TRANSCRIPT_LIMIT')
    return _strip_injected('\n'.join(bits))


def normalize(messages, user_content, assistant_content, *, anchor, previous=None, rewound=False, rewind_after=None):
    """Require a complete live snapshot anchored by the current user turn, not a summary.

    The caller owns session lineage and the stable absolute anchor. No time is read here and
    no transcript or API sidecar is looked up from disk. Later turns may repeat text verbatim.
    """
    if not isinstance(messages, list) or not messages or len(messages) > MAX_ROWS:
        raise TranscriptError('UNPROVEN_SNAPSHOT')
    if not isinstance(user_content, str) or not isinstance(assistant_content, str):
        raise TranscriptError('UNPROVEN_SNAPSHOT')
    fallback = _timestamp(anchor)
    user, reply = _strip_injected(user_content), _strip_injected(assistant_content)
    if not user or not reply or _WITHHELD in user or _WITHHELD in reply:
        raise TranscriptError('UNPROVEN_CURRENT_TURN')
    turns = []
    size = 0
    current = None
    current_media = False
    current_timestamp = None

    def add(role, content, raw_time=None):
        nonlocal size
        try:
            index = len(turns)
            time = _timestamp(raw_time) if raw_time is not None else fallback + timedelta(milliseconds=index)
            timestamp = _iso(time)
            if previous is not None and index < len(previous):
                old = previous[index]
                if old['role'] == role and old['content'] == content:
                    timestamp = old['timestamp']
            entry = {'role': role, 'content': content, 'timestamp': timestamp}
            size += len(json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 1
        except (OverflowError, UnicodeError):
            raise TranscriptError('INVALID_TRANSCRIPT') from None
        if size > MAX_TRANSCRIPT_BYTES or len(turns) >= MAX_TURNS:
            raise TranscriptError('TRANSCRIPT_LIMIT')
        turns.append(entry)

    for row in messages:
        if not isinstance(row, dict):
            raise TranscriptError('UNPROVEN_SNAPSHOT')
        if row.get('_is_context_summary') is True or is_compaction_summary_message(row):
            raise TranscriptError('COMPACTED_TRANSCRIPT')
        role = row.get('role')
        if role not in ('user', 'assistant') or row.get('display_kind') in _SKIP_KINDS or withheld_prompt_text(row) is not None:
            continue
        content = _text(row.get('content'))  # api_content is never a transcript source
        if content == COMPRESSION_CONTINUATION_USER_CONTENT:
            raise TranscriptError('COMPACTED_TRANSCRIPT')
        if _WITHHELD in content:
            continue
        raw_time = row.get('timestamp')
        if role == 'user':
            current = len(turns) if content else None
            current_media = isinstance(row.get('content'), list)
            current_timestamp = raw_time
        if content:
            if role == 'assistant' and not turns:
                raise TranscriptError('UNPROVEN_SNAPSHOT')
            add(role, content, raw_time)
        if role == 'assistant' and isinstance(row.get('tool_calls'), list):
            calls = row['tool_calls']
            if len(calls) > MAX_TURNS:
                raise TranscriptError('TRANSCRIPT_LIMIT')
            for call in calls:
                function = call.get('function') if isinstance(call, dict) else None
                name = function.get('name') if isinstance(function, dict) else None
                if isinstance(name, str) and _TOOL_NAME.fullmatch(name):
                    if not turns:
                        raise TranscriptError('UNPROVEN_SNAPSHOT')
                    add('action', name, raw_time)

    if current is None or turns[current]['content'] != (_IMAGE_STAMP.sub('', user) if current_media else user):
        raise TranscriptError('UNPROVEN_CURRENT_TURN')
    if rewind_after is not None and _timestamp(current_timestamp) <= _timestamp(rewind_after):
        raise TranscriptError('STALE_REWIND_SNAPSHOT')
    if turns[-1]['role'] != 'assistant' or turns[-1]['content'] != reply:
        add('assistant', reply)
    if previous is not None and not rewound and turns[:len(previous)] != previous:
        raise TranscriptError('NON_PREFIX_SNAPSHOT')
    if previous and rewound:
        # Trusted rewind can remove multiple tail turns, never rewrite the retained prefix.
        if rewind_after is None:
            raise TranscriptError('UNPROVEN_REWIND')
        shared = min(current, len(previous))
        if turns[:shared] != previous[:shared] or (current < len(previous) and previous[current]['role'] != 'user'):
            raise TranscriptError('NON_PREFIX_REWIND')
    return turns
