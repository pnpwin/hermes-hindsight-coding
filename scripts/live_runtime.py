"""Managed Hermes subprocess for explicitly authorized local Hindsight verification."""
import asyncio
from contextlib import asynccontextmanager, contextmanager, nullcontext
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import sys
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.parse import quote
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
SCOPE_BANKS = {'disposable': r'ht-hermes-parity-[a-z0-9][a-z0-9-]{7,47}',
               'existing-read': r'[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}'}
API_ORIGIN = 'http://127.0.0.1:8888'
DOC = 'quartz-relay-fixture-architecture'
LIMIT = 2**20
RESERVATION = r'[0-9a-f]{32}'
RESERVATION_LABEL = 'Hermes verifier reservation: '


def valid_page_tree(status, raw):
    if status != 200:
        return False
    try:
        tree = json.loads(raw)
        if not isinstance(tree, dict) or not isinstance(tree.get('roots'), list):
            return False
        stack = list(tree['roots'])
        while stack:
            node = stack.pop()
            if (not isinstance(node, dict) or node.get('kind') not in {'page', 'folder'}
                    or not all(isinstance(node.get(k), str) and node[k] for k in ('id', 'name'))):
                return False
            if 'children' in node and node['children'] is not None:
                if not isinstance(node['children'], list):
                    return False
                stack.extend(node['children'])
        return True
    except (ValueError, TypeError, RecursionError):
        return False


def require(condition, message):
    if not condition:
        raise ValueError(message)


def upstream(method, path, body=None):
    conn = HTTPConnection('127.0.0.1', 8888, timeout=24)
    try:
        conn.request(method, path, body, {'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = conn.getresponse()
        raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT and not 300 <= response.status < 400, 'upstream response refused')
        return response.status, raw
    finally:
        conn.close()


class BankAccess:
    def __init__(self, bank, scope, reservation=None):
        require(re.fullmatch(SCOPE_BANKS.get(scope, r'(?!)'), bank), 'invalid live bank or scope')
        self.bank, self.scope = bank, scope
        self.reservation = reservation
        self.prefix = '/v1/default/banks/' + quote(bank, safe='')

    def client_allowed(self, method, target):
        target = re.sub(r'%[0-9a-fA-F]{2}', lambda m: m[0].upper(), target)
        if method == 'GET' and target == self.prefix + '/pages/tree':
            return True
        if self.scope != 'disposable':
            return False
        if target in ('/version', '/v1/version'):
            return method == 'GET'
        if not target.startswith(self.prefix + '/'):
            return False
        suffix = target[len(self.prefix):]
        if method == 'GET':
            return suffix == '/config' or bool(re.fullmatch(r'/operations/[a-f0-9-]{36}', suffix))
        return method == 'POST' and suffix in {'/memories', '/memories/recall', '/reflect'}

    def request(self, method, suffix):
        # Provisioning belongs to the operator; even the disposable control plane is read-only.
        require(self.scope == 'disposable', 'control path unavailable in existing-read')
        valid = method == 'GET' and (suffix in {'/config', '/documents?limit=100', '/operations?limit=100'}
                                    or re.fullmatch(r'/(documents|operations)/[A-Za-z0-9%:-]+', suffix))
        require(valid, 'invalid control path')
        status, body = upstream(method, self.prefix + suffix)
        require(status == 200, f'control read/write {method} {suffix} returned HTTP {status}')
        return json.loads(body)

    def setup(self):
        self.verify_reservation()
        for suffix, rows in [('/documents?limit=100', 'items'), ('/operations?limit=100', 'operations')]:
            listing = self.request('GET', suffix)
            require(type(listing.get('total')) is int and listing['total'] == 0 and listing.get(rows) == [],
                    'reserved bank is not empty')

    def verify_reservation(self):
        reservation = self.reservation or ''
        require(re.fullmatch(RESERVATION, reservation), 'disposable bank has no reservation')
        reply = self.request('GET', '/config')
        overrides = reply.get('overrides') or {}
        expected = RESERVATION_LABEL + reservation
        require(reply.get('bank_id') == self.bank and overrides.get('retain_mission') == expected,
                'disposable bank reservation does not match')
        config = reply['config']
        strategies = config.get('retain_strategies') or {}
        require(config.get('retain_mission') == expected and config.get('enable_observations') is False
                and config.get('store_document_text') is True
                and all(isinstance(strategies.get(key), dict) and strategies[key].get('retain_mission')
                        and strategies[key].get('retain_extraction_mode') == 'concise' for key in ('conversation','document')),
                'disposable bank strategies or document policy do not match')


class Proxy:
    def __init__(self, access, hermes_marker, codex_marker):
        self.bank = access.bank
        self.audit, self.retain = [], []
        self.tree_checks = []
        self.armed = False
        self.lock = Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def parse_request(self):
                if not super().parse_request():
                    return False
                method, target, *_ = self.requestline.split()
                proxy.audit.append([method, target])
                if not proxy.armed or not access.client_allowed(method, target):
                    self.send_error(403)
                    return False
                return True

            def relay(self):
                row = None
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    require(0 <= size <= LIMIT and not self.headers.get('Transfer-Encoding'), 'request length')
                    body = self.rfile.read(size) if size else None
                    if self.command == 'POST' and self.path == access.prefix + '/memories':
                        if body is None:
                            raise ValueError('missing retain body')
                        item, = json.loads(body)['items']
                        strategy, doc, content, tags = (item[k] for k in ('strategy', 'document_id', 'content', 'tags'))
                        if strategy == 'conversation':
                            require(re.fullmatch(r'conversation:hermes-[a-f0-9]{64}', doc)
                                    and {'source:chat', 'harness:hermes'} <= set(tags)
                                    and content.count(hermes_marker) == 1, 'conversation guard')
                        else:
                            require(strategy == 'document' and doc == DOC
                                    and {'source:upload', 'harness:codex'} <= set(tags)
                                    and content.count(codex_marker) == 1, 'document guard')
                        row = {'strategy': strategy, 'document_id': doc, 'tags': tags,
                               'sha256': sha256(content.encode()).hexdigest()}
                        with proxy.lock:
                            require(strategy not in {x['strategy'] for x in proxy.retain}, 'duplicate retain')
                            access.verify_reservation()
                            proxy.retain.append(row)
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.send_error(422)
                    return
                try:
                    status, raw = upstream(self.command, self.path, body)
                    if access.scope == 'existing-read':
                        proxy.tree_checks.append(valid_page_tree(status, raw))
                    if row is not None and status == 200:
                        row['operation_id'] = str(UUID(json.loads(raw)['operation_id']))
                except (OSError, ValueError, KeyError, TypeError):
                    self.send_error(502)
                    return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = relay
            do_POST = relay

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def await_document(access, row, marker):
    operation = row['operation_id']
    deadline = monotonic() + 75
    while monotonic() < deadline:
        state = access.request('GET', '/operations/' + operation)
        require(state['operation_id'] == operation, 'operation identity changed')
        status = state['status']
        if status == 'completed':
            children = state.get('child_operations', [])
            require(len(children) == 1 and children[0]['status'] == 'completed', 'retain child not completed')
            break
        require(status not in {'failed', 'canceled', 'cancelled', 'not_found'}, f'operation {operation}: {status}')
        sleep(2)
    else:
        raise TimeoutError(f'operation {operation} did not complete')
    doc = access.request('GET', '/documents/' + quote(row['document_id'], safe=''))
    body = doc['original_text']
    require(doc['bank_id'] == access.bank and doc['id'] == row['document_id'] and body.count(marker) == 1
            and sha256(body.encode()).hexdigest() == row['sha256'] and set(row['tags']) <= set(doc['tags'])
            and doc['memory_unit_count'] > 0, 'canonical document did not match submitted retain')
    return row | {'memory_unit_count': doc['memory_unit_count'], 'status': 'completed'}


@asynccontextmanager
async def oracle(fixture, config, proxy):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import TextContent
    from test_bridge_client import NODE
    from test_codex_oracle import MCP
    home = fixture.home / 'oracle-parity'
    home.mkdir(mode=0o700, exist_ok=True)
    diag = home / 'diag.jsonl'
    params = StdioServerParameters(command=str(NODE), args=[str(MCP)], cwd=str(fixture.cwd), env={
        'HOME': str(home), 'TMPDIR': str(home), 'PATH': '/usr/bin:/bin', 'HINDSIGHT_CONFIG': str(config),
        'HINDSIGHT_MCP_HARNESS': 'codex', 'HINDSIGHT_LOG_FILE': str(home/'mcp.log'), 'HINDSIGHT_DIAG_FILE': str(diag),
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
    })
    async with stdio_client(params) as (rd, wr), ClientSession(rd, wr) as client:
        async with asyncio.timeout(15):
            await client.initialize()

        async def call(name, args):
            async with asyncio.timeout(36):
                result = await client.call_tool(name, args)
            texts = [x.text for x in result.content if isinstance(x, TextContent)]
            require(not result.is_error and texts and len(texts) == len(result.content),
                    f'{name} returned an error or non-text receipt')
            return json.loads('\n'.join(texts))

        value = await call('hindsight_diagnose', {})
        value = value.get('data', value)
        require(value['bank_id'] == proxy.bank and value['config']['api_url'] == proxy.origin
                and value['config']['path'] == str(config) and value['environment']['diagnostics_file'] == str(diag),
                'Codex MCP diagnostic changed scope')
        yield call


async def exercise(fixture, access, proxy, markers):
    from gateway.session_context import scoped_current_session_id
    from test_lifecycle import local_session
    policy = fixture.home / 'plugin-data/hermes-hindsight-coding/config.json'
    config = Path(json.loads(policy.read_bytes())['config_path'])
    config.write_text(json.dumps({'serverMode': 'self-hosted', 'apiUrl': proxy.origin, 'autoSeed': False,
        'codebaseSurvey': False, 'autoInject': 'recall' if access.scope == 'disposable' else 'none',
        'recallOptions': {'types':['world','experience'], 'budget':'mid', 'max_tokens':2000},
        'retainSessions': access.scope == 'disposable', 'optInOnly': True, 'mapPathToBank': {str(fixture.repo):access.bank}}))
    settings = {'config_path':str(config), 'allowed_roots':[str(fixture.repo)], 'allowed_api_url':proxy.origin,
                'writes':False, 'seed':False, 'survey':False, 'manage_bank_config':False}
    policy.write_text(json.dumps(settings))
    probe = fixture.make_agent(fixture.cwd, 'parity-readonly-preflight')
    with local_session(fixture.cwd), scoped_current_session_id(probe.session_id):
        diag = fixture.invoke(probe, 'hindsight_diagnose', {})
        require(diag['bank_id'] == access.bank and diag['api_url'] == proxy.origin, 'Hermes diagnostic changed scope')
    read_errors = []
    async with oracle(fixture, config, proxy) as call:
        require(not proxy.audit, 'preflight unexpectedly used HTTP')
        if access.scope == 'existing-read':
            proxy.armed = True
            with local_session(fixture.cwd), scoped_current_session_id(probe.session_id):
                try:
                    fixture.invoke(probe, 'hindsight_list_pages', {})
                except (AssertionError, ValueError, KeyError):
                    read_errors.append('Hermes')
                finally:
                    probe._memory_manager.shutdown_all()
            try:
                await call('hindsight_list_pages', {})
            except (ValueError, KeyError):
                read_errors.append('Codex')
    if access.scope == 'existing-read':
        require(proxy.tree_checks == [True, True] and not read_errors, 'invalid page-tree response from live Hindsight')
        require(proxy.audit == [['GET',access.prefix+'/pages/tree']]*2, 'unexpected existing-read requests')
        return {'page_tree_requests':2, 'retain_requests':0}
    access.setup()
    policy.write_text(json.dumps(settings | {'writes':True}))
    proxy.armed = True
    sid = 'parity-writer-' + uuid4().hex
    agent = fixture.make_agent(fixture.cwd, sid)
    manager = agent._memory_manager
    h, c = markers
    question = 'For this synthetic repository, what ledger and deployment identifier did we decide to use for Fable Queue?'
    answer = f'Final architecture decision: the Fable Queue audit ledger uses SQLite. The committed deployment identifier is {h}.'
    messages = [{'role':'user', 'content':question}, {'role':'assistant', 'content':answer}]
    with local_session(fixture.cwd), scoped_current_session_id(sid):
        diag = fixture.invoke(agent, 'hindsight_diagnose', {})
        require(diag['bank_id'] == access.bank and diag['api_url'] == proxy.origin, 'writer scope mismatch')
        manager.sync_all(question, answer, session_id=sid, messages=messages)
        require(manager.flush_pending(timeout=23), 'Hermes write queue timed out')
        row, = proxy.retain
        require(row['strategy'] == 'conversation', 'Hermes did not retain the conversation')
        saved_h = await_document(access, row, h)
        before = access.request('GET','/operations?limit=100')
        manager.sync_all(question, answer, session_id=sid, messages=messages)
        require(manager.flush_pending(timeout=23), 'Hermes replay queue timed out')
        after = access.request('GET','/operations?limit=100')
        require(proxy.audit.count(['POST',access.prefix+'/memories']) == 1
                and before['operations'] == after['operations'] and before['total'] == after['total'], 'Hermes replay changed storage')
    async with oracle(fixture, config, proxy) as call:
        reflect = await call('hindsight_reflect', {'query':'What is the committed deployment identifier for the Fable Queue audit ledger? Quote the exact identifier.'})
        require(h in json.dumps(reflect), 'Codex did not return the Hermes control value')
        reply = await call('hindsight_ingest_document', {'title':'Quartz Relay Fixture Architecture',
            'content':f'Quartz Relay uses the Archive Encoder. In this synthetic repository its fixed diagnostic route identifier is {c}.'})
        require((reply.get('data') or reply).get('doc_id') == DOC, 'Codex ingest did not bind the expected document')
    require(len(proxy.retain) == 2 and proxy.retain[1]['strategy'] == 'document', 'missing Codex retain')
    saved_c = await_document(access, proxy.retain[1], c)
    with local_session(fixture.cwd), scoped_current_session_id(sid):
        memory = manager.prefetch_all('What is the diagnostic route identifier for the Quartz Relay Archive Encoder?', session_id=sid)
        require(c in memory, 'Hermes did not return the Codex control value')
        manager.shutdown_all()
    listing = access.request('GET','/documents?limit=100')
    require(listing['total'] == len(listing['items']) == 2 and {x['id'] for x in listing['items']} == {row['document_id'],DOC},
            'unexpected stored documents')
    require(proxy.audit.count(['POST',access.prefix+'/memories']) == 2 and all(access.client_allowed(*req) for req in proxy.audit),
            'unexpected outbound requests')
    return {'documents':[saved_h,saved_c], 'codex_read_hermes':True, 'hermes_read_codex':True}


@contextmanager
def reservation_lock(scratch, bank):
    filename = scratch / ('hindsight-verifier-' + sha256(bank.encode()).hexdigest() + '.lock')
    fd = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, 'unsafe reservation lock')
        flock(fd, LOCK_EX | LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    if (len(sys.argv) not in {4,5} or sys.argv[1] not in SCOPE_BANKS or sys.argv[2] != sys.argv[3]
            or not re.fullmatch(SCOPE_BANKS[sys.argv[1]],sys.argv[2])
            or (sys.argv[1] == 'disposable' and (len(sys.argv) != 5 or not re.fullmatch(RESERVATION, sys.argv[4])))
            or (sys.argv[1] == 'existing-read' and len(sys.argv) != 4)):
        print('invalid or unconfirmed live scope', file=sys.stderr)
        raise SystemExit(2)
    scope, bank, _, *reservation = sys.argv[1:]
    scratch = Path.home().resolve() / '.hermes/cache/scratch'
    home = Path(os.environ.get('HERMES_HOME', '')).resolve()
    require(home.parent == scratch and home.name.startswith('hindsight-live-home-') and home.is_dir(),
            'live worker requires the launcher scratch HERMES_HOME')
    sys.path.insert(0, str(ROOT / 'tests'))
    from runtime_cases import RuntimeTest
    access = BankAccess(bank, scope, *reservation)
    markers = tuple(prefix + uuid4().hex[:12] for prefix in ('violet-orbit-', 'cobalt-summit-'))
    proxy = Proxy(access, *markers)

    fixture = RuntimeTest()
    try:
        fixture.setUp()
        with reservation_lock(scratch, bank) if scope == 'disposable' else nullcontext():
            result = asyncio.run(exercise(fixture, access, proxy, markers))
        print(json.dumps({'endpoint':API_ORIGIN, 'bank':bank, **result}, sort_keys=True))
        print(f'PASS scope={scope} bank={bank} provider=hermes-hindsight-coding')
    finally:
        proxy.close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
