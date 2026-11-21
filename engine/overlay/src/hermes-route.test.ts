import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as bank from './core/bank';
import { resolveHermesRoute } from './hermes-route';
import { directoryPin } from './hermes-git-io';

let root: string;
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-C', cwd, ...args], {
  env: { PATH: '/usr/bin:/bin', HOME: root, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' },
  stdio: 'pipe', timeout: 8000,
});
beforeEach(() => { root = realpathSync(mkdtempSync(join(tmpdir(), 'hermes-route-'))); });
afterEach(() => { vi.restoreAllMocks(); rmSync(root, { recursive: true, force: true }); });

function dir(name: string) {
  const path = join(root, name);
  mkdirSync(path, { recursive: true });
  return path;
}

const raw = { serverMode: 'self-hosted' as const, apiUrl: 'http://127.0.0.1:1' };
const route = (cwd: string, startCwd = cwd, overrides: Record<string, unknown> = {}, allowedRoots = [root]) =>
  resolveHermesRoute({ cwd, startCwd, rootPins: allowedRoots.map((r) => directoryPin(realpathSync(r))),
    routingHome: dir('operator'), rawConfig: { ...raw, ...overrides } });

describe('Hermes route using upstream Coding Agents', () => {
  it('keeps the start directory outside Git and preserves punctuation', () => {
    const start = dir('Plain.v1');
    const cwd = dir('Plain.v1/nested');
    expect(route(cwd, start)).toMatchObject({ bankId: 'coding-agent::Plain.v1', projectRoot: null });
  });

  it('matches an ordinary repository and a linked worktree, with an independent physical allowlist', () => {
    const repo = dir('Probe.v1');
    git(repo, 'init', '-q', '--template=');
    git(repo, '-c', 'user.email=fixture@example.invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture', '--allow-empty');
    const wt = join(root, 'Link');
    git(repo, 'worktree', 'add', '--detach', '-q', wt);
    expect(route(dir('Probe.v1/sub'))).toMatchObject({ bankId: 'coding-agent::Probe.v1' });
    expect(route(wt)).toMatchObject({ bankId: 'coding-agent::Probe.v1' });
    expect(route(wt, wt, {}, [wt])).toBeNull();
  });

  it('preserves the bare repository basename', () => {
    const bare = dir('Probe.git');
    git(bare, 'init', '-q', '--template=', '--bare');
    expect(route(bare)).toMatchObject({ bankId: 'coding-agent::Probe.git' });
  });

  it('keeps the conventional hidden bare hub and rejects a foreign bare pointer', () => {
    const hub = dir('hub');
    const bare = dir('hub/.bare');
    git(bare, 'init', '-q', '--template=', '--bare');
    writeFileSync(join(hub, '.git'), 'gitdir: ./.bare\n');
    expect(route(hub)).toMatchObject({ bankId: 'coding-agent::hub' });
    const foreign = dir('foreign');
    writeFileSync(join(foreign, '.git'), `gitdir: ${bare}\n`);
    expect(route(foreign)).toBeNull();
  });

  it('refuses a failed Git probe even if a static route could be guessed', () => {
    const cwd = dir('transient');
    vi.spyOn(bank, 'resolveProjectRoot').mockReturnValue({ status: 'failed', reason: 'fixture' });
    expect(route(cwd, cwd, { bankId: 'not-approved-without-a-probe' })).toBeNull();
  });

  it('refuses external gitdir, commondir and symlinks without using them to derive a bank', () => {
    const approved = dir('approved');
    const external = dir('external');
    git(external, 'init', '-q', '--template=');
    const gitPath = join(approved, '.git');
    writeFileSync(gitPath, `gitdir: ${join(external, '.git')}\n`);
    expect(route(approved, approved, {}, [approved])).toBeNull();
    rmSync(gitPath);
    mkdirSync(gitPath);
    writeFileSync(join(gitPath, 'commondir'), `${join(external, '.git')}\n`);
    expect(route(approved, approved, {}, [approved])).toBeNull();
    rmSync(gitPath, { recursive: true });
    symlinkSync(join(external, '.git'), gitPath);
    expect(route(approved, approved, {}, [approved])).toBeNull();
  });

  it('uses the physical workspace for path mapping and opt-in', () => {
    const first = dir('first');
    const second = dir('second');
    const alias = join(first, 'link');
    symlinkSync(second, alias);
    const optIn = { optInOnly: true, mapPathToBank: { [first]: 'first-bank' } };
    expect(route(alias, second, optIn, [first, second])).toBeNull();
    expect(route(alias, second, { ...optIn, mapPathToBank: {
      [first]: 'first-bank', [second]: 'second-bank',
    } }, [first, second])).toMatchObject({ bankId: 'second-bank' });
  });

  it('refuses an unrelated directory pointing at the administrative area of a real worktree', () => {
    const first = dir('first');
    git(first, 'init', '-q', '--template=');
    git(first, '-c', 'user.email=fixture@example.invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture', '--allow-empty');
    const linked = join(root, 'link');
    git(first, 'worktree', 'add', '--detach', '-q', linked);
    const unrelated = dir('unrelated');
    writeFileSync(join(unrelated, '.git'), readFileSync(join(linked, '.git')));
    expect(route(linked)).toMatchObject({ bankId: 'coding-agent::first' });
    expect(route(unrelated)).toBeNull();
  });

  it('rejects malformed or dangling nested .git markers instead of inheriting the parent bank', () => {
    const parent = dir('parent');
    git(parent, 'init', '-q', '--template=');
    const nested = dir('parent/nested');
    for (const marker of ['not-a-git-indirection', 'gitdir: ./missing-dir\n', 'text\ngitdir: ../.git\n']) {
      writeFileSync(join(nested, '.git'), marker);
      expect(route(nested)).toBeNull();
    }
  });

  it('rejects oversized metadata instead of guessing a different bank', () => {
    const cwd = dir('over-limit');
    writeFileSync(join(cwd, '.git'), Buffer.alloc(64 * 1024 + 1));
    expect(route(cwd)).toBeNull();
    rmSync(join(cwd, '.git'));
    git(cwd, 'init', '-q', '--template=');
    writeFileSync(join(cwd, '.git/config'), Buffer.alloc(64 * 1024 + 1));
    expect(route(cwd)).toBeNull();
  });

  it('uses Hermes overrides, tilde mapping, bank alias and the unmodified Codex opt-in order', () => {
    const cwd = dir('operator/src');
    const config = {
      optInOnly: true,
      harnesses: { codex: { disabled: true } },
      mapPathToBank: { '~/src': 'project.bank' },
      paths: { '~/src': { autoInject: 'pages' } },
      banks: { 'project.bank': { bank: 'canonical.project', retainSessions: false } },
    };
    expect(route(cwd, cwd, config)).toMatchObject({
      bankId: 'canonical.project', cfg: { autoInject: 'pages', retainSessions: false },
    });
    expect(route(dir('denied'), undefined, config)).toBeNull();
    expect(route(cwd, cwd, { ...config, harnesses: { hermes: { disabled: true } } })).toBeNull();
  });
});
