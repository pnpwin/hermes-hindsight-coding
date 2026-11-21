import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { directoryPin } from './hermes-git-io';
import { readGitMessages } from './hermes-seed';

let temp: string, repo: string, worker: string;
function git(...args: string[]) {
  return execFileSync('/usr/bin/git', ['-C', repo, ...args], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    env: { PATH: '/usr/bin:/bin', HOME: worker, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', LC_ALL: 'C' },
  }).trim();
}
function commit(message: string) {
  git('-c', 'user.name=Fixture Author', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', message, '--allow-empty');
}

beforeEach(() => {
  temp = realpathSync(mkdtempSync(join(tmpdir(), 'hermes-seed-')));
  repo = join(temp, 'repository'); worker = join(temp, 'worker');
  mkdirSync(repo); mkdirSync(worker, { mode: 0o700 });
  git('init', '-q', '--template=');
});
afterEach(() => rmSync(temp, { recursive: true, force: true }));

function snapshot(limit = 3) { return readGitMessages({ project: directoryPin(repo), worker, limit }); }

describe('read-only seed snapshot', () => {
  it('formats the upstream message-only window at one HEAD, without file contents', () => {
    writeFileSync(join(repo, 'fixture.txt'), 'synthetic secret in the diff');
    git('add', 'fixture.txt'); commit('Initial fixture'); commit('Second fixture\n\nRationale on second');
    const head = git('rev-parse', 'HEAD');
    const result = snapshot();
    expect(result).toMatchObject({ head, repoName: 'repository' });
    expect(result?.content).toContain('Initial fixture');
    expect(result?.content).toContain('Rationale on second');
    expect(result?.content).toContain('Fixture Author');
    expect(result?.content).not.toMatch(/synthetic secret|fixture\.txt|fixture@example/);
    expect(snapshot(1)?.content).not.toContain('Initial fixture');
  });

  it('ignores source config, grafts, hooks and packed object sidecars', () => {
    commit('Before packing');
    git('repack', '-ad');
    const admin = join(repo, '.git');
    git('config', 'include.path', join(temp, 'invalid-config'));
    writeFileSync(join(temp, 'invalid-config'), '[not a git config');
    writeFileSync(join(admin, 'objects/info/alternates'), `${join(temp, 'outside')}\n`);
    expect(snapshot()?.content).toContain('Before packing');
  });

  it('rejects an object-directory alias rather than reading it', () => {
    commit('In scope');
    const objects = join(repo, '.git', 'objects');
    const admin = join(repo, '.git');
    const outside = join(temp, 'outside');
    mkdirSync(outside);
    symlinkSync(outside, join(objects, 'ab'));
    expect(() => snapshot()).toThrow('unsafe Git objects');
    expect(realpathSync(admin)).toBe(admin);
  });

  it('does not seed an unborn repo', () => { expect(snapshot()).toBeNull(); });
});
