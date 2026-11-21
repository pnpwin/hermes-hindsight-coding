"""Fresh-process integration for disposable verifier with a stateful fake upstream."""
import asyncio
from collections import Counter
import json
from urllib.parse import unquote
from uuid import uuid4
import unittest
from unittest.mock import patch

import runtime_cases
from test_live_runtime import BANK, RESERVATION, load_worker, reserved_config


class DisposableCase(unittest.TestCase):
    def test_existing_read_rejects_false_empty_trees_from_both_clients(self):
        worker = load_worker()
        for code, body in [(200, b'{"roots":[]}'), (404, b'{"detail":"not found"}'),
                           (200, b'broken JSON'), (200, b'{"roots":{}}'), (200, b'{"roots":[42]}')]:
            with self.subTest(code=code, body=body):
                fixture = runtime_cases.RuntimeTest()
                try:
                    fixture.setUp()
                    access = worker.BankAccess(BANK, 'existing-read')
                    proxy = worker.Proxy(access, 'unused-h', 'unused-c')
                    try:
                        expected = ('GET',access.prefix+'/pages/tree')
                        with patch.object(worker, 'upstream', return_value=(code, body)) as upstream:
                            if (code, body) == (200, b'{"roots":[]}'):
                                result = asyncio.run(worker.exercise(fixture, access, proxy, ('unused-h','unused-c')))
                                self.assertEqual(result, {'page_tree_requests':2, 'retain_requests':0})
                                self.assertEqual(proxy.audit, [list(expected)]*2)
                            else:
                                with self.assertRaisesRegex(ValueError, 'page-tree response'):
                                    asyncio.run(worker.exercise(fixture, access, proxy, ('unused-h','unused-c')))
                                self.assertIn(False, proxy.tree_checks)
                            self.assertTrue(upstream.called)
                            self.assertEqual({call.args[:2] for call in upstream.call_args_list}, {expected})
                    finally:
                        proxy.close()
                finally:
                    fixture.doCleanups()

    def test_full_disposable_flow(self):
        worker = load_worker()
        fixture = runtime_cases.RuntimeTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        access = worker.BankAccess(BANK, 'disposable', RESERVATION)
        markers = ('violet-fixture-abcd', 'cobalt-fixture-efgh')
        proxy = worker.Proxy(access, *markers)

        self.addCleanup(proxy.close)
        docs, operations, trace = {}, {}, []

        def fake_api(method, path, payload=None):
            trace.append((method, path))
            body = json.loads(payload) if payload else {}
            if path in {'/v1/version', '/version'}:
                value = {'api_version':'1.2.1'}
            else:
                self.assertTrue(path == access.prefix or path.startswith(access.prefix+'/'), (method,path))
                suffix = path[len(access.prefix):]
                if method == 'GET' and suffix == '/config':
                    value = reserved_config(worker)
                elif method == 'GET' and suffix.startswith('/documents?'):
                    value = {'total':len(docs),'items':list(docs.values())}
                elif method == 'GET' and suffix.startswith('/documents/'):
                    value = docs[unquote(suffix.split('/')[-1])]
                elif method == 'GET' and suffix.startswith('/operations?'):
                    value = {'total':len(operations),'operations':list(operations.values())}
                elif method == 'GET' and suffix.startswith('/operations/'):
                    value = operations[suffix.split('/')[-1]]
                elif method == 'GET' and suffix == '/pages/tree':
                    value = {'roots':[]}
                elif method == 'POST' and suffix == '/memories':
                    item, = body['items']
                    doc = item['document_id']
                    self.assertNotIn(doc, docs)
                    docs[doc] = {'id':doc, 'bank_id':BANK, 'original_text':item['content'], 'tags':item['tags'],
                                 'memory_unit_count':1}
                    op = body.get('operation_id') or str(uuid4())
                    operations[op] = {'operation_id':op, 'status':'completed','child_operations':[{'status':'completed'}]}
                    value = {'operation_id':op}
                elif method == 'POST' and suffix == '/reflect':
                    value = {'text':'\n'.join(doc['original_text'] for doc in docs.values())}
                elif method == 'POST' and suffix == '/memories/recall':
                    value = {'results':[{'text':doc['original_text']} for doc in docs.values()]}
                else:
                    self.fail(f'unexpected fixture request {method} {path}')
            return 200, json.dumps(value).encode()

        with patch.object(worker, 'upstream', side_effect=fake_api):
            result = asyncio.run(worker.exercise(fixture, access, proxy, markers))
        self.assertEqual([x['strategy'] for x in result['documents']], ['conversation','document'])
        self.assertTrue(result['codex_read_hermes'] and result['hermes_read_codex'])
        self.assertEqual(Counter(p for m,p in trace if m in {'PUT','PATCH','POST'}), Counter({
            access.prefix+'/memories':2,
            access.prefix+'/reflect':1, access.prefix+'/memories/recall':1,
        }))
