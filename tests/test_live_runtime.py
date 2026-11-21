"""Negative and positive controls for the live verifier using an isolated transport."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BANK = 'ht-hermes-parity-fixture-0001'
RESERVATION = '1'*32


def reserved_config(worker, bank=BANK):
    marker = worker.RESERVATION_LABEL + RESERVATION
    strategies = {name: {'retain_mission': 'extract fixture decision', 'retain_extraction_mode':'concise'}
                  for name in ('conversation','document')}
    return {'bank_id':bank, 'overrides':{'retain_mission':marker}, 'config':{
        'retain_mission':marker, 'retain_strategies':strategies,
        'enable_observations':False, 'store_document_text':True,
    }}


def load_worker():
    spec = importlib.util.spec_from_file_location('live_runtime', ROOT / 'scripts/live_runtime.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveRuntimeTest(unittest.TestCase):
    def test_parallel_local_runs_cannot_share_one_reservation_lock(self):
        from tempfile import TemporaryDirectory

        worker = load_worker()
        with TemporaryDirectory() as d, worker.reservation_lock(Path(d), BANK):
            with self.assertRaises(BlockingIOError), worker.reservation_lock(Path(d), BANK):
                self.fail('second verifier acquired the same bank lock')

    def test_disposable_claim_without_operator_marker_cannot_mutate(self):
        worker = load_worker()
        access = worker.BankAccess(BANK, 'disposable', RESERVATION)
        trace = []

        def existing_or_raced(method, path, body=None):
            trace.append((method, path))
            if path.endswith('/config') and method == 'GET':
                return 200, json.dumps({'bank_id': BANK, 'config': {}, 'overrides': {}}).encode()
            if path.startswith('/v1/default/banks?'):
                return 200, b'{"total":0,"banks":[]}'
            return 200, b'{}'

        with patch.object(worker, 'upstream', side_effect=existing_or_raced):
            with self.assertRaises(ValueError):
                access.setup()
        self.assertTrue(all(method == 'GET' for method, _ in trace), trace)

    def test_disposable_flow_with_real_clients_and_stateful_fake_api(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/py-test.py'), 'live_verifier_cases.py'],
                                cwd=ROOT, capture_output=True, text=True, timeout=75)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('test_full_disposable_flow', result.stderr)
        self.assertIn('test_existing_read_rejects_false_empty_trees_from_both_clients', result.stderr)
        self.assertRegex(result.stderr, r'(?m)^Ran 2 tests in ')

    def test_worker_rejects_unconfirmed_or_unsafe_invocation(self):
        for params in [('disposable', BANK, 'wrong'), ('disposable', 'production', 'production'),
                       ('existing-read', '../other', '../other')]:
            with self.subTest(params=params):
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/live_runtime.py'), *params],
                                      capture_output=True, text=True, timeout=8)
                self.assertEqual(proc.returncode, 2)
                self.assertIn('invalid or unconfirmed live scope', proc.stderr)

    def test_guard_pins_bank_and_prevents_writes_in_existing_read(self):
        worker = load_worker()
        read = worker.BankAccess('coding-agent::project', 'existing-read')
        page = '/v1/default/banks/coding-agent%3A%3Aproject/pages/tree'
        self.assertTrue(read.client_allowed('GET', page))
        self.assertTrue(read.client_allowed('GET', page.replace('%3A', '%3a')))
        for method, path in [('POST', page), ('GET', page+'?bank=other'), ('GET', page.replace('project', 'other')),
                             ('GET', '/v1/version'), ('POST', read.prefix+'/reflect'), ('PATCH', read.prefix+'/config')]:
            self.assertFalse(read.client_allowed(method, path), (method, path))

    def test_disposable_proxy_requires_current_reservation_and_rejects_duplicates(self):
        from http.client import HTTPConnection
        from uuid import UUID

        worker = load_worker()
        access = worker.BankAccess(BANK, 'disposable', RESERVATION)
        proxy = worker.Proxy(access, 'test-marker-h', 'test-marker-c')
        self.addCleanup(proxy.close)
        endpoint = access.prefix + '/memories'
        item = {'strategy': 'conversation', 'document_id': 'conversation:hermes-'+'a'*64,
                'content': 'test-marker-h', 'tags': ['source:chat', 'harness:hermes']}
        body = json.dumps({'items': [item]})
        receipt = {'operation_id': str(UUID(int=1))}
        owner = {'reserved':False}

        def post(path, data):
            conn = HTTPConnection('127.0.0.1', proxy.server.server_port, timeout=4)
            try:
                conn.request('POST', path, body=data)
                res = conn.getresponse()
                res.read()
                return res.status
            finally:
                conn.close()

        def respond(method, target, body=None):
            if method == 'GET':
                return 200, json.dumps(reserved_config(worker) if owner['reserved'] else {'bank_id':BANK, 'overrides':{}}).encode()
            return 200, json.dumps(receipt).encode()

        with patch.object(worker, 'upstream', side_effect=respond) as relay:
            self.assertEqual(post(endpoint, body), 403)
            proxy.armed = True
            self.assertEqual(post(endpoint.replace(BANK, 'other'), body), 403)
            self.assertEqual(post(endpoint, '{}'), 422)
            self.assertEqual(post(endpoint, body), 422)
            owner['reserved'] = True
            self.assertEqual(post(endpoint, body), 200)
            self.assertEqual(post(endpoint, body), 422)
            self.assertEqual([x.args[:2] for x in relay.call_args_list], [('GET', access.prefix+'/config')]*2 + [('POST', endpoint)])
            self.assertEqual(proxy.audit, [['POST', endpoint], ['POST', endpoint.replace(BANK, 'other')],
                                           *([['POST', endpoint]]*4)])
