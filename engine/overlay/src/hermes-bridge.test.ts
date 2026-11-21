import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { handleRequest, handleWire, resolveBoundRoute } from './hermes-bridge';
import { directoryPin } from './hermes-git-io';


let fixture: string, cwd: string, owner: string, config: string;
const origin = 'http://127.0.0.1:1';
const input = () => ({ cwd, startCwd: cwd, rootPins: [directoryPin(realpathSync(cwd))], routingHome: owner, allowedApiUrl: origin });
const request = (args: unknown = input()) => ({ v: 1, op: 'resolve', args });
const put = (data: Record<string, unknown> = {}) => writeFileSync(config, JSON.stringify({
  serverMode: 'self-hosted', apiUrl: origin, ...data,
}));

beforeEach(() => {
  fixture = realpathSync(mkdtempSync(join(tmpdir(), 'hermes-wire-')));
  cwd = join(fixture, 'Probe.v1');
  owner = join(fixture, 'owner');
  mkdirSync(cwd);
  mkdirSync(owner);
  config = join(fixture, 'coding-agent.json');
  put();
});
afterEach(() => { vi.unstubAllGlobals(); rmSync(fixture, { recursive: true, force: true }); });

describe('Hermes bridge v1', () => {
  it('resolves without requests and returns a safe route only', () => {
    const fetch = vi.fn(() => { throw new Error('unexpected network'); });
    vi.stubGlobal('fetch', fetch);
    put({ apiToken: 'fixture-credential', recallOptions: { max_tokens: 2000 } });
    const reply = handleRequest(request(), config);
    expect(reply).toMatchObject({ v: 1, ok: true, data: {
      active: true, bankId: 'coding-agent::Probe.v1', apiUrl: origin,
      configIdentity: expect.stringMatching(/^[0-9a-f]{64}$/),
    } });
    expect(JSON.stringify(reply)).not.toContain('fixture-credential');
    expect(JSON.stringify(reply)).not.toContain('recallOptions');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects invalid wire, unknown operations and caller-selected bank', () => {
    for (const bad of [null, { v: 2, op: 'resolve', args: input() }, { v: 1, op: 'retain', args: input() },
      request({ ...input(), bankId: 'another-bank' }), request({ ...input(), rootPins: [] })]) {
      expect(handleRequest(bad, config)).toMatchObject({ v: 1, ok: false, error: { code: 'INVALID_REQUEST' } });
    }
    const duplicate = Buffer.from(JSON.stringify(request()).replace('"v":1', '"v":1,"v":1'));
    expect(handleWire(duplicate, config)).toMatchObject({ ok: false, error: { code: 'INVALID_REQUEST' } });
  });

  it('rejects hostile or unsupported endpoints after upstream path and bank overrides', () => {
    for (const patch of [{ serverMode: 'daemon' }, { apiUrl: 'http://127.0.0.1:2' },
      { banks: { 'coding-agent::Probe.v1': { apiUrl: 'https://example.invalid' } } },
      { paths: { [cwd]: { apiUrl: 'http://127.0.0.1:2' } } },
      { bankId: '..' }]) {
      put(patch);
      expect(handleRequest(request(), config)).toMatchObject({ ok: false });
    }
  });

  it('rejects malformed, duplicate-key and symlinked config snapshots, without using defaults', () => {
    for (const raw of ['{', '{"disabled":true,"disabled":false}', '{"harnesses":[]}', '{"__proto__":{}}']) {
      writeFileSync(config, raw);
      expect(handleRequest(request(), config)).toMatchObject({ ok: false, error: { code: 'CONFIG_INVALID' } });
    }
    put();
    const link = join(fixture, 'symlink.json');
    symlinkSync(config, link);
    expect(handleRequest(request(), link)).toMatchObject({ ok: false, error: { code: 'CONFIG_INVALID' } });
  });

  it('rechecks the bound identity instead of switching bank or using changed config', () => {
    const reply = handleRequest(request(), config);
    if (!reply.ok || !reply.data.active) throw new Error('missing route');
    expect(resolveBoundRoute(input(), config, reply.data)).toMatchObject({ bankId: reply.data.bankId });
    put({ autoInject: 'none' });
    expect(resolveBoundRoute(input(), config, reply.data)).toBeNull();
  });
});
