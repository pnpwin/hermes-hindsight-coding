import json
import sys
from threading import Event
from time import monotonic, time

import test_prefetch
from test_lifecycle import local_session


TURNS = [
    {'role': 'user', 'content': 'first question', 'timestamp': '2026-04-05T00:00:00.000Z'},
    {'role': 'assistant', 'content': 'first answer', 'timestamp': '2026-04-05T00:00:01.000Z'},
]
MORE = TURNS + [
    {'role': 'user', 'content': 'second question', 'timestamp': '2026-04-05T00:01:00.000Z'},
    {'role': 'assistant', 'content': 'second answer', 'timestamp': '2026-04-05T00:01:01.000Z'},
]


class RetainBridgeTest(test_prefetch.PrefetchFixture):
    def setUp(self):
        super().setUp()
        self.api.bank_config = {'store_document_text': True, 'retain_strategies': {'conversation': {}}}
        self.api.retain_response = lambda request: {'operation_id': request['operation_id']}
        self.p = self.activate('turns')
        self.mod = sys.modules[type(self.p).__module__]
        self.client = self.p._bridge
        with local_session(self.cwd):
            self.binding = self.mod.route_descriptor(self.p.acquire_work('turns', writing=True))

    def stop(self, turns, rewound=False):
        self.client.retain(self.binding, session_id='turns', turns=turns, rewound=rewound)

    def writes(self):
        return [body for method, path, body in self.api.requests if method == 'POST' and path.endswith('/memories')]

    def test_real_worker_retains_once_and_appends_with_strategy_and_provenance(self):
        self.stop(TURNS)
        self.stop(TURNS)
        self.stop(MORE)
        base, append = self.writes()
        self.assertEqual(base['items'][0]['strategy'], 'conversation')
        doc = base['items'][0]['document_id']
        self.assertRegex(doc, r'^conversation:hermes-[a-f0-9]{64}$')
        self.assertEqual(base['items'][0]['metadata']['harness'], 'hermes')
        self.assertEqual(json.loads(base['items'][0]['content'].splitlines()[0])['content'], 'REF-ID: ' + doc)
        self.assertEqual(append['items'][0]['document_id'], doc)
        self.assertEqual(append['items'][0]['update_mode'], 'append')
        self.assertEqual([json.loads(line) for line in append['items'][0]['content'].splitlines()], MORE[2:])
        self.assertNotEqual(base['operation_id'], append['operation_id'])

    def test_no_retain_without_strategy_or_for_changed_route(self):
        self.api.bank_config = {'retain_strategies': {}}
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(TURNS)
        self.assertFalse(self.writes())
        self.binding['bankId'] = 'some-other-bank'
        self.api.requests.clear()
        with self.assertRaisesRegex(self.mod.BridgeError, 'ROUTE_REJECTED'):
            self.stop(TURNS)
        self.assertEqual(self.api.requests, [])

    def test_opted_in_hindsight_121_uses_bank_defaults_without_mutating_config(self):
        self.api.bank_config = {'retain_strategies': {}, 'store_document_text':True}
        policy = self.home/'plugin-data/hermes-hindsight-coding/config.json'
        policy.write_text(json.dumps(json.loads(policy.read_text()) | {'allow_bank_default_strategy':True}))
        self.api.version = '1.2.0'
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(TURNS)
        self.assertEqual(self.writes(), [])
        self.api.version = '1.2.1'
        self.stop(TURNS)
        self.stop(TURNS)
        body, = self.writes()
        self.assertEqual(body['items'][0]['strategy'], 'conversation')
        self.assertFalse(any(m in ('PUT','PATCH','DELETE') for m,_ in self.api.wire_requests))

    def test_memory_manager_supplies_only_scrubbed_turns_and_never_writes_twice(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        manager.add_provider(self.p)
        token = 'sk-proj-' + 'aB123cdEF4' * 8
        user = f'please do not retain the fixture credential {token}'
        rows = [{'role': 'user', 'content': user, 'api_content': 'INJECTED_ONLY'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'function': {
                    'name': 'terminal', 'arguments': '{"command":"TOOL_SECRET"}'}}]},
                {'role': 'tool', 'content': 'RESULT_SECRET'}, {'role': 'assistant', 'content': 'done'}]
        with local_session(self.cwd):
            manager.sync_all(user, 'done', session_id='turns', messages=rows)
            self.assertTrue(manager.flush_pending(timeout=10))
            manager.sync_all(user, 'done', session_id='turns', messages=rows)
            self.assertTrue(manager.flush_pending(timeout=10))
            self.p.on_session_end(rows)
        body, = self.writes()
        wire = json.dumps(body)
        for secret in (token, 'INJECTED_ONLY', 'TOOL_SECRET', 'RESULT_SECRET'):
            self.assertNotIn(secret, wire)
        self.assertIn('terminal', wire)

    def test_undo_replaces_same_document_and_ignores_the_old_queued_snapshot(self):
        older = [{'role': 'user', 'content': 'original', 'timestamp': time() - 2},
                 {'role': 'assistant', 'content': 'old reply', 'timestamp': time() - 1}]
        with local_session(self.cwd):
            self.p.sync_turn('original', 'old reply', session_id='turns', messages=older)
            self.p.on_session_switch('turns', rewound=True)
            self.p.sync_turn('original', 'old reply', session_id='turns', messages=older)
            newer = [{'role': 'user', 'content': 'correction', 'timestamp': time()}]
            self.p.sync_turn('correction', 'replacement', session_id='turns', messages=newer)
        before, after = [post['items'][0] for post in self.writes()]
        self.assertEqual(before['document_id'], after['document_id'])
        self.assertNotIn('update_mode', after)
        self.assertNotIn('old reply', after['content'])
        self.assertIn('replacement', after['content'])

    def test_memory_manager_data_prefixed_text_never_reaches_http_or_cursor(self):
        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        manager.add_provider(self.p)
        secret = 'data: ' + 'sk-proj-' + 'bC123deFG4' * 8
        self.stop(TURNS)
        self.api.requests.clear()
        self.api.retain_response = lambda _: {}  # unsafe append would persist pending bytes
        cursor = next(self.client.worker_home.glob('retain-cursors/*.json'))
        previous = cursor.read_bytes()
        forms = [
            (secret, secret),
            ('  ' + secret, '  ' + secret),
            ('<memory-context>discard</memory-context>' + secret, '<memory-context>discard</memory-context>' + secret),
            ('da<memory-context>discard</memory-context>ta: fixture', 'da<memory-context>discard</memory-context>ta: fixture'),
            ('reply\n' + secret, [{'type': 'text', 'text': 'reply'}, {'type': 'output_text', 'text': '  ' + secret}]),
        ]
        for index, (final, content) in enumerate(forms):
            with self.subTest(form=index), local_session(self.cwd):
                manager.sync_all('second question', final, session_id='turns', messages=[*MORE[:3], {'role': 'assistant', 'content': content}])
                self.assertTrue(manager.flush_pending(timeout=10))
                self.assertEqual(self.api.requests, [])
                self.assertEqual(cursor.read_bytes(), previous)

    def test_direct_bridge_rejects_untrusted_text_before_http_and_cursor_mutation(self):
        self.stop(TURNS)
        self.api.requests.clear()
        cursor = next(self.client.worker_home.glob('retain-cursors/*.json'))
        previous = cursor.read_bytes()
        self.api.retain_response = lambda _: {}
        for content in ('data: raw fixture', '\u00a0DATA: raw fixture', 'note\r\n  data: raw fixture'):
            with self.subTest(content=content):
                with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
                    self.stop([*MORE[:3], MORE[3] | {'content': content}])
                self.assertEqual(self.api.requests, [])
                self.assertEqual(cursor.read_bytes(), previous)

    def test_worker_will_not_replay_unsafe_pending_state_from_older_build(self):
        self.stop(TURNS)
        self.api.retain_response = lambda _: {}
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        cursor = next(self.client.worker_home.glob('retain-cursors/*.json'))
        state = json.loads(cursor.read_text())
        entry = state['pending'][0]
        rows = [json.loads(line) for line in entry['content'].splitlines()]
        rows[-1]['content'] = ' \n data: raw legacy fixture'
        entry['content'] = '\n'.join(map(json.dumps, rows))
        cursor.write_text(json.dumps(state))
        self.api.requests.clear()
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        self.assertEqual(self.api.requests, [])

    def test_pending_append_replays_with_identical_id_after_worker_restarts(self):
        received = {}
        document = []
        def apply(request):
            op_id = request['operation_id']
            if op_id not in received:
                received[op_id] = 0
                document.extend(request['items'][0]['content'].splitlines())
            received[op_id] += 1
            # First append is applied by the fixture but its receipt is lost/invalid.
            if request['items'][0].get('update_mode') == 'append' and received[op_id] == 1:
                return {}
            return {'operation_id': op_id}
        self.api.retain_response = apply
        self.stop(TURNS)
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        cursor = next(self.client.worker_home.glob('retain-cursors/*.json'))
        pending, = json.loads(cursor.read_text())['pending']
        self.stop(MORE)
        self.assertEqual(received[pending['operationId']], 2)
        self.assertEqual(len(document), 5)  # REF-ID plus four messages, no append twice

    def test_pending_replay_rejects_duplicate_keys_before_http(self):
        self.stop(TURNS)
        self.api.retain_response = lambda _: {}
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        cursor = next(self.client.worker_home.glob('retain-cursors/*.json'))
        state = json.loads(cursor.read_bytes())
        pending = state['pending'][0]
        valid = pending['content']
        for key in ('content', r'\u0063ontent'):
            with self.subTest(key=key):
                pending['content'] = valid.replace('"content":', f'"{key}":"data: fixture marker","content":', 1)
                cursor.write_text(json.dumps(state))
                before = cursor.read_bytes()
                self.api.requests.clear()
                with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
                    self.stop(MORE)
                self.assertEqual(self.api.requests, [])
                self.assertEqual(cursor.read_bytes(), before)

    def test_no_wire_write_after_policy_revocation(self):
        path = self.home / 'plugin-data/hermes-hindsight-coding/config.json'
        path.write_text(json.dumps(json.loads(path.read_text()) | {'writes': False}))
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_DISABLED'):
            self.stop(TURNS)
        self.assertEqual(self.api.requests, [])

    def test_upstream_retain_disabled_is_checked_before_transcript_enters_worker(self):
        from unittest.mock import patch

        path = self.p.settings.config_path
        path.write_text(json.dumps(json.loads(path.read_text()) | {'retainSessions': False}))
        p = self.activate('disabled')
        with local_session(self.cwd), patch.object(p._bridge, '_request', wraps=p._bridge._request) as send:
            p.sync_turn('first question', 'first answer', session_id='disabled', messages=TURNS)
        self.assertTrue(all(call.args[0] != 'retain' for call in send.call_args_list))
        self.assertFalse(self.api.requests)

    def test_retain_never_posts_without_confirmed_idempotency(self):
        self.api.version = None
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(TURNS)
        self.assertEqual(self.writes(), [])

    def test_duplicate_stop_is_noop_on_replace_only_bank(self):
        self.api.bank_config = {'store_document_text': False, 'retain_strategies': {'conversation': {}}}
        self.stop(TURNS)
        self.stop(TURNS)
        self.assertEqual(len(self.writes()), 1)

    def test_identical_append_bytes_at_different_positions_have_distinct_ids(self):
        self.stop(TURNS)
        self.stop(MORE)
        self.stop(MORE + MORE[2:])
        _, first, second = self.writes()
        self.assertEqual(first['items'][0]['content'], second['items'][0]['content'])
        self.assertNotEqual(first['operation_id'], second['operation_id'])

    def test_429_without_receipt_does_not_block_the_next_snapshot(self):
        self.api.retain_response = lambda _: (429, {'Retry-After': '60'}, {})
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(TURNS)
        rejected, = self.writes()
        self.api.operation_lookup = lambda op: 404 if op == rejected['operation_id'] else 200
        self.api.retain_response = lambda request: {'operation_id': request['operation_id']}
        self.stop(MORE)
        self.assertEqual(len(self.writes()), 2)

    def test_unconfirmed_append_blocks_undo_until_it_settles(self):
        self.stop(TURNS)
        self.api.retain_response = lambda _: {}
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        self.api.operation_status = 'pending'
        self.api.retain_response = lambda request: {'operation_id': request['operation_id']}
        correction = [TURNS[0], TURNS[1] | {'content': 'corrected'}]
        pending_id = self.writes()[-1]['operation_id']
        self.api.operation_lookup = lambda op: 404 if op == pending_id else 200
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(correction, rewound=True)
        self.assertEqual(len(self.writes()), 2)
        self.api.operation_lookup = lambda _: 200
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(correction, rewound=True)
        self.assertEqual(len(self.writes()), 2)
        self.api.operation_status = 'completed'
        self.stop(correction, rewound=True)
        self.assertEqual(len(self.writes()), 3)

    def test_lost_replace_receipt_replays_same_operation_id(self):
        self.api.retain_response = lambda _: {}
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(TURNS)
        self.api.retain_response = lambda request: {'operation_id': request['operation_id']}
        self.stop(TURNS)
        first, second = self.writes()
        self.assertEqual(first, second)

    def test_failed_async_operation_repairs_document_without_appending(self):
        self.stop(TURNS)
        self.api.operation_status = 'failed'
        self.stop(MORE)
        first, repair = self.writes()
        self.assertNotIn('update_mode', repair['items'][0])
        self.assertEqual([json.loads(line) for line in repair['items'][0]['content'].splitlines()[1:]], MORE)
        self.assertNotEqual(first['operation_id'], repair['operation_id'])

    def test_rewind_does_not_overtake_an_accepted_operation(self):
        self.stop(TURNS)
        correction = [TURNS[0], TURNS[1] | {'content': 'corrected'}]
        self.api.operation_status = 'pending'
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(correction, rewound=True)
        self.assertEqual(len(self.writes()), 1)
        self.api.operation_status = 'completed'
        self.stop(correction, rewound=True)
        self.assertEqual(len(self.writes()), 2)

    def test_retry_after_preserves_operation_id_and_never_waits_past_deadline(self):
        attempts = 0
        def response(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return 429, {'Retry-After': '0.05'}, {}
            return {'operation_id': request['operation_id']}

        self.api.retain_response = response
        self.stop(TURNS)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.writes()[0], self.writes()[1])
        self.api.retain_response = lambda _: (429, {'Retry-After': '60'}, {})
        started = monotonic()
        with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_FAILED'):
            self.stop(MORE)
        self.assertLess(monotonic() - started, 10)
        self.assertEqual(len(self.writes()), 3)
        rejected = self.writes()[-1]['operation_id']
        self.api.operation_lookup = lambda op: 404 if op == rejected else 200
        self.api.retain_response = lambda request: {'operation_id': request['operation_id']}
        self.stop([TURNS[0], TURNS[1] | {'content': 'corrected'}], rewound=True)
        self.assertNotIn('update_mode', self.writes()[-1]['items'][0])

    def test_two_workers_cannot_claim_same_cursor(self):
        from concurrent.futures import ThreadPoolExecutor

        arrived, release = Event(), Event()
        def response(request):
            arrived.set()
            release.wait(timeout=3)
            return {'operation_id': request['operation_id']}

        self.api.retain_response = response
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.stop, TURNS)
            try:
                self.assertTrue(arrived.wait(timeout=5))
                with self.assertRaisesRegex(self.mod.BridgeError, 'RETAIN_BUSY_OR_UNSAFE'):
                    self.stop(TURNS)
            finally:
                release.set()
            first.result(timeout=8)
        self.assertEqual(len(self.writes()), 1)
