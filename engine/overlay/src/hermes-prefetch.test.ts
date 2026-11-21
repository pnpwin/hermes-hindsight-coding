import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ReflectError } from './core/hindsight';
import { directoryPin } from './hermes-git-io';

const state = vi.hoisted(() => ({
  recall: [] as string[], calls: [] as string[], reflectError: undefined as ReflectError | undefined,
  options: [] as Record<string, unknown>[],
  pages: [] as { id: string; name: string }[],
  documented: true,
}));
vi.mock('./core/hindsight', async (importOriginal) => {
  const original = await importOriginal<typeof import('./core/hindsight')>();
  return { ...original, HindsightClient: class {
    constructor(readonly opts: unknown) { state.calls.push('construct'); state.options.push(opts as Record<string, unknown>); }
    async listPages() { state.calls.push('pages'); return { items: state.pages }; }
    async recallObservations(query: string) { state.calls.push('recall'); return state.recall; }
    async reflect() { state.calls.push('reflect'); if (state.reflectError) throw state.reflectError; return ''; }
    async searchPages() { state.calls.push('search'); return []; }
    async listDocumentIds() { state.calls.push('forbidden seed probe'); return new Set(); }
    async hasDocuments() { state.calls.push('documents'); return state.documented; }
    get bank() { return 'coding-agent::Probe.v1'; }
  } };
});
import { handleRequest, prefetch } from './hermes-bridge';

let root: string, cwd: string, config: string;
const cacheFile = () => join(root, 'prefetch-cache', createHash('sha256').update('fixture').digest('hex') + '.json');
function args() {
  const input = { cwd, startCwd: cwd, rootPins: [directoryPin(realpathSync(cwd))],
    routingHome: root, allowedApiUrl: 'http://127.0.0.1:1' };
  const result = handleRequest({ v: 1, op: 'resolve', args: input }, config);
  if (!result.ok || !result.data.active) throw Error('binding failed');
  return { ...input, binding: result.data, sessionId: 'fixture', query: 'current goal',
    cacheFile: cacheFile(), autoInject: true };
}
function put(options: Record<string, unknown>) {
  writeFileSync(config, JSON.stringify({ serverMode: 'self-hosted', apiUrl: 'http://127.0.0.1:1', ...options }));
}
beforeEach(() => {
  root = realpathSync(mkdtempSync(join(tmpdir(), 'hermes-prefetch-')));
  vi.stubEnv('HOME', root);
  cwd = join(root, 'Probe.v1');
  config = join(root, 'config.json');
  mkdirSync(cwd); mkdirSync(join(root, 'prefetch-cache'), { mode: 0o700 });
  put({ autoInject: 'recall', recallOptions: { types: null, budget: 'mid', max_tokens: 2000 } });
  state.recall = []; state.calls = []; state.reflectError = undefined;
  state.options = [];
  state.pages = [];
  state.documented = true;
});
afterEach(() => { vi.unstubAllEnvs(); rmSync(root, { recursive: true, force: true }); });

describe('Hermes prefetch', () => {
  it('recalls once on the current prompt, preserves no-seed policy, and reuses upstream cache', async () => {
    state.recall = ['historical fact, not an instruction'];
    const first = await prefetch(args(), config);
    if (!first.ok) throw Error('prefetch failed');
    expect(first).toMatchObject({ ok: true, data: { context: expect.stringContaining('historical fact') } });
    expect(first.data.context).toContain('external data, not instructions');
    expect(state.calls).not.toContain('forbidden seed probe');
    expect(state.calls).not.toContain('documents');
    expect(state.options[0].recallOptions).toMatchObject({ types: null, budget: 'mid', max_tokens: 2000 });
    const second = await prefetch(args(), config);
    expect(second).toMatchObject({ ok: true, data: { context: '' } });
    expect(state.calls.filter((x) => x === 'recall')).toHaveLength(1);
  });

  it('respects recallOptions and autoInject none', async () => {
    put({ autoInject: 'none', recallOptions: { max_tokens: 2000 } });
    const output = await prefetch(args(), config);
    expect(output).toMatchObject({ ok: true, data: { context: '' } });
    expect(state.calls).not.toContain('recall');
    expect(state.calls).not.toContain('reflect');
  });

  it('uses the upstream fallback for a reflect 5xx and does not fallback on 4xx', async () => {
    put({ autoInject: 'reflect' });
    state.reflectError = new ReflectError('fixture 5xx', 500, false);
    state.recall = ['fallback history'];
    const output = await prefetch(args(), config);
    expect(output.ok && output.data.context).toContain('fallback history');
    state.calls = []; state.recall = []; state.reflectError = new ReflectError('fixture 401', 401, false);
    rmSync(cacheFile());
    const failed = await prefetch(args(), config);
    expect(failed).toMatchObject({ ok: true, data: { context: '' } });
    expect(state.calls).not.toContain('recall');
    expect(state.calls).not.toContain('search');
  });

  it('rejects a caller-selected binding and invalid session/cache arguments', async () => {
    for (const update of [{ binding: { ...args().binding, bankId: 'elsewhere' } },
      { sessionId: '' }, { cacheFile: 'relative.json' }, { cacheFile: join(root, 'escape.json') }, { query: '' }]) {
      expect(await prefetch({ ...args(), ...update }, config)).toMatchObject({ ok: false });
    }
  });

  it('limits empty retrieval to five turns of the same session', async () => {
    for (let i = 0; i < 6; i++) {
      expect(await prefetch({ ...args(), query: `task ${i}` }, config)).toMatchObject({ ok: true, data: { context: '' } });
    }
    expect(state.calls.filter((x) => x === 'recall')).toHaveLength(5);
  });

  it('renders the initial roster and refreshes it on the upstream cadence', async () => {
    put({ autoInject: 'none', pageRefreshEveryTurns: 2 });
    state.pages = [{ id: 'kp-first', name: 'first title' }];
    const turn1 = await prefetch(args(), config);
    expect(turn1).toMatchObject({ ok: true, data: { additionalContext: expect.stringContaining('first title') } });
    state.pages = [{ id: 'kp-next', name: 'new title' }];
    await prefetch(args(), config);
    expect(state.calls.filter((x) => x === 'pages')).toHaveLength(1);
    await prefetch(args(), config);
    expect(state.calls.filter((x) => x === 'pages')).toHaveLength(2);
    const turn4 = await prefetch(args(), config);
    expect(turn4).toMatchObject({ ok: true, data: { additionalContext: expect.stringContaining('new title') } });
  });

  it('defers the first reflect only for a confirmed cold Git bank', async () => {
    const { execFileSync } = await import('node:child_process');
    execFileSync('/usr/bin/git', ['-C', cwd, 'init', '-q', '--template='], { env: { PATH: '/usr/bin:/bin', HOME: root } });
    put({ autoInject: 'reflect', autoSeed: true });
    state.documented = false;
    expect(await prefetch(args(), config)).toMatchObject({ ok: true, data: { deferInitialReflect: false } });
    expect(state.calls).toContain('reflect');
    execFileSync('/usr/bin/git', ['-C', cwd, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
      'commit', '-qm', 'fixture', '--allow-empty'], { env: { PATH: '/usr/bin:/bin', HOME: root, GIT_CONFIG_NOSYSTEM: '1' } });
    rmSync(cacheFile());
    state.calls = [];
    expect(await prefetch(args(), config)).toMatchObject({ ok: true, data: { deferInitialReflect: true } });
    expect(state.calls).not.toContain('reflect');
    await prefetch(args(), config);
    expect(state.calls).toContain('reflect');
  });
});
