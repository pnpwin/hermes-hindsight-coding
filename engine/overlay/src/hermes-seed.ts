import { execFileSync } from 'node:child_process';
import { constants, closeSync, fstatSync, lstatSync, mkdirSync, mkdtempSync, openSync, opendirSync,
  readSync, rmSync, writeFileSync, writeSync } from 'node:fs';
import { basename, join } from 'node:path';
import { renderGitMessageWindow } from './core/git';
import { assertDirectoryPin, directoryPin, readScopedHead, scopedGitIO, type DirectoryPin } from './hermes-git-io';

const MAX_ENTRIES = 8192;
const MAX_OBJECT_BYTES = 64 * 1024 * 1024;
const MAX_TEXT_BYTES = 256 * 1024;

export type GitMessages = { repoName: string; head: string; timestamp: string; content: string };

export function currentSeedHead(project: DirectoryPin): string | null {
  assertDirectoryPin(project);
  const admin = join(project.path, '.git');
  const io = scopedGitIO([project], [project.path]);
  if (io.entryKind(admin) !== 'dir' || io.entryKind(join(admin, 'commondir')) !== 'none') return null;
  return readScopedHead(io, admin, admin);
}

/** Give Git only a private bare graph. Never evaluate repository config or open alternates. */
export function readGitMessages({ project, worker, limit }: { project: DirectoryPin; worker: string; limit: number }): GitMessages | null {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000) throw Error('invalid seed limit');
  assertDirectoryPin(project);
  const trustedWorker = directoryPin(worker);
  const admin = join(project.path, '.git');
  const io = scopedGitIO([project], [project.path]);
  if (io.entryKind(admin) !== 'dir' || io.entryKind(join(admin, 'commondir')) !== 'none') return null;
  const head = readScopedHead(io, admin, admin);
  if (!head || head.length !== 40) return null;
  const adminPin = directoryPin(admin);
  const source = join(admin, 'objects');
  const sourcePin = directoryPin(source);
  const local = mkdtempSync(join(worker, 'git-seed-'));
  const deadline = Date.now() + 4000;
  let entries = 0, copied = 0;
  const slab = Buffer.allocUnsafe(128 * 1024);
  const end = () => { if (Date.now() >= deadline) throw Error('seed deadline'); };

  function files(dir: string, pin: DirectoryPin, consume: (name: string) => void) {
    assertDirectoryPin(pin);
    const handle = opendirSync(dir);
    try {
      for (let entry = handle.readSync(); entry; entry = handle.readSync()) {
        end();
        if (++entries > MAX_ENTRIES) throw Error('seed object limit');
        consume(entry.name);
      }
    } finally { handle.closeSync(); }
    assertDirectoryPin(pin);
  }
  function copy(name: string, from: DirectoryPin, to: string) {
    assertDirectoryPin(project);
    assertDirectoryPin(adminPin);
    assertDirectoryPin(sourcePin);
    assertDirectoryPin(from);
    const path = join(from.path, name);
    const candidate = lstatSync(path, { bigint: true });
    const equal = (value: typeof candidate) => value.isFile() && value.nlink === 1n && value.dev === candidate.dev &&
      value.ino === candidate.ino && value.size === candidate.size && value.ctimeNs === candidate.ctimeNs;
    if (!equal(candidate)) throw Error('unsafe Git object');
    const size = Number(candidate.size);
    copied += size;
    if (!Number.isSafeInteger(copied) || copied > MAX_OBJECT_BYTES) throw Error('seed object limit');
    const src = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      assertDirectoryPin(from);
      if (!equal(fstatSync(src, { bigint: true })) || !equal(lstatSync(path, { bigint: true }))) throw Error('Git object changed');
      const dst = openSync(join(to, name), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
      try {
        for (let remaining = size; remaining > 0;) {
          end();
          const n = readSync(src, slab, 0, Math.min(remaining, slab.length), null);
          if (!n) throw Error('Git object changed');
          for (let p = 0; p < n;) p += writeSync(dst, slab, p, n - p);
          remaining -= n;
        }
      } finally { closeSync(dst); }
      if (!equal(fstatSync(src, { bigint: true }))) throw Error('Git object changed');
      assertDirectoryPin(from);
    } finally { closeSync(src); }
  }
  try {
    assertDirectoryPin(trustedWorker);
    mkdirSync(join(local, 'refs'), { mode: 0o700 });
    const target = join(local, 'objects');
    mkdirSync(target, { mode: 0o700 });
    writeFileSync(join(local, 'HEAD'), `${head}\n`, { mode: 0o600, flag: 'wx' });
    writeFileSync(join(local, 'config'), '[core]\nrepositoryformatversion = 0\nbare = true\n', { mode: 0o600, flag: 'wx' });
    files(source, sourcePin, (name) => {
      if (name !== 'pack' && !/^[0-9a-f]{2}$/.test(name)) return;
      let pin: DirectoryPin;
      try { pin = directoryPin(join(source, name)); } catch { throw Error('unsafe Git objects'); }
      const dest = join(target, name);
      mkdirSync(dest, { mode: 0o700 });
      const match = name === 'pack' ? /^pack-[a-f0-9]{40}\.(pack|idx)$/ : /^[a-f0-9]{38}$/;
      files(pin.path, pin, (file) => { if (match.test(file)) copy(file, pin, dest); });
    });
    if (readScopedHead(io, admin, admin) !== head) throw Error('Git HEAD changed');
    function read(...argv: string[]): string {
      end();
      const out = execFileSync('/usr/bin/git', ['--git-dir', local, '--no-pager', '--no-replace-objects',
        '-c', 'log.showSignature=false', '-c', 'core.abbrev=7', ...argv], {
        cwd: local, timeout: Math.max(1, deadline - Date.now()), killSignal: 'SIGKILL', maxBuffer: MAX_TEXT_BYTES,
        stdio: ['ignore', 'pipe', 'pipe'], env: { PATH: '/usr/bin:/bin', HOME: local, XDG_CONFIG_HOME: local,
          GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1', GIT_NO_LAZY_FETCH: '1', GIT_ALLOW_PROTOCOL: '', LC_ALL: 'C' },
      });
      return new TextDecoder('utf-8', { fatal: true }).decode(out);
    }
    const content = renderGitMessageWindow(read, head, limit);
    if (!content) return null;
    const timestamp = read('log', '-n', '1', '--no-merges', '--format=%aI', head, '--').trim();
    if (!Number.isFinite(Date.parse(timestamp)) || Buffer.byteLength(content) > MAX_TEXT_BYTES) throw Error('invalid seed snapshot');
    assertDirectoryPin(project);
    assertDirectoryPin(adminPin);
    assertDirectoryPin(sourcePin);
    if (readScopedHead(io, admin, admin) !== head) throw Error('Git HEAD changed');
    return { repoName: basename(project.path), head, timestamp, content };
  } finally { rmSync(local, { recursive: true, force: true }); }
}
