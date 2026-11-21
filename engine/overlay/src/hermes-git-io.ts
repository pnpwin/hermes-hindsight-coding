import { closeSync, constants, fstatSync, lstatSync, openSync, readSync, realpathSync, statSync } from 'node:fs';
import type { BigIntStats } from 'node:fs';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import type { GitProbeIO } from './core/git-layout';

const MAX_METADATA_BYTES = 64 * 1024;
const MAX_PROBE_OPERATIONS = 1024;
export type DirectoryPin = { path: string; dev: string; ino: string };

export function within(directory: string, roots: string[]) {
  return roots.some((root) => {
    const child = relative(root, directory);
    return child === '' || (!isAbsolute(child) && child !== '..' && !child.startsWith(`..${sep}`));
  });
}

export function directoryPin(path: string): DirectoryPin {
  if (!isAbsolute(path) || realpathSync(path) !== path) throw Error('untrusted directory');
  const st = lstatSync(path, { bigint: true });
  if (!st.isDirectory()) throw Error('untrusted directory');
  return { path, dev: String(st.dev), ino: String(st.ino) };
}

export function assertDirectoryPin(pin: DirectoryPin) {
  const live = directoryPin(pin.path);
  if (live.dev !== pin.dev || live.ino !== pin.ino) throw Error('directory identity changed');
}

function sameMetadata(a: BigIntStats, b: BigIntStats) {
  return a.dev === b.dev && a.ino === b.ino && a.nlink === b.nlink && a.size === b.size &&
    a.ctimeNs === b.ctimeNs && a.mtimeNs === b.mtimeNs;
}

/** No Git commands or repository configuration evaluation. Upstream interprets only bounded, in-scope bytes. */
export function scopedGitIO(pins: DirectoryPin[], starts: string[]): GitProbeIO {
  const roots = pins.map((pin) => pin.path);
  let remaining = MAX_PROBE_OPERATIONS;
  function tick() { if (--remaining < 0) throw Error('Git probe limit'); }
  function metadata(path: string) {
    try { return lstatSync(path, { bigint: true }); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
      throw error;
    }
  }
  function checked(path: string, expected: BigIntStats) {
    pins.forEach(assertDirectoryPin);
    if (!within(path, roots) || realpathSync(path) !== path || !sameMetadata(statSync(path, { bigint: true }), expected)) {
      throw Error('Git metadata escaped its scope');
    }
  }
  const io: GitProbeIO = {
    entryKind(path) {
      tick();
      if (!within(path, roots)) {
        // An enclosing repo outside the allowlist changes the identity. Inspect only marker
        // existence in ancestor directories, never dereference/read them or external pointers.
        const parent = path.slice(0, path.lastIndexOf(sep)) || sep;
        if (!starts.some((start) => within(start, [parent])) || !['.git', 'HEAD', 'objects', 'refs'].includes(basename(path))) {
          throw Error('out-of-scope Git pointer');
        }
        if (metadata(path)) throw Error('potential enclosing Git layout is out of scope');
        return 'none';
      }
      const st = metadata(path);
      if (!st) return 'none';
      checked(path, st);
      if (st.isDirectory()) return 'dir';
      if (st.isFile()) return 'file';
      throw Error('unsupported Git metadata kind');
    },
    canonical(path) {
      tick();
      if (!within(path, roots) || realpathSync(path) !== path) throw Error('out-of-scope Git directory');
      return path;
    },
    readText(path) {
      tick();
      if (!within(path, roots)) throw Error('out-of-scope Git metadata');
      const before = metadata(path);
      if (!before) return null;
      if (!before.isFile() || before.nlink !== 1n || before.size > BigInt(MAX_METADATA_BYTES)) throw Error('invalid Git metadata');
      checked(path, before);
      const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        if (!sameMetadata(fstatSync(fd, { bigint: true }), before)) throw Error('Git metadata changed before read');
        checked(path, before);
        const bytes = Buffer.alloc(MAX_METADATA_BYTES + 1);
        let used = 0;
        while (used < bytes.length) {
          const n = readSync(fd, bytes, used, bytes.length - used, null);
          if (!n) break;
          used += n;
        }
        if (used > MAX_METADATA_BYTES || !sameMetadata(fstatSync(fd, { bigint: true }), before)) throw Error('Git metadata changed');
        checked(path, before);
        return new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, used));
      } finally { closeSync(fd); }
    },
    validateMarker(holder, adminDir, layout, pointer) {
      const admin = io.canonical(adminDir);
      const common = layout.commonDir;
      if (io.entryKind(join(admin, 'HEAD')) !== 'file' ||
          io.entryKind(join(common, 'objects')) !== 'dir' || io.entryKind(join(common, 'refs')) !== 'dir') {
        throw Error('incomplete Git layout');
      }
      if (!pointer) {
        if (admin !== common || io.entryKind(join(admin, 'commondir')) !== 'none') throw Error('redirected Git directory');
        return;
      }
      if (admin === common && layout.bare && dirname(common) === holder && basename(common).startsWith('.') &&
          io.entryKind(join(admin, 'commondir')) === 'none') return; // conventional hidden bare hub
      if (dirname(admin) !== join(common, 'worktrees')) throw Error('unsupported Git indirection');
      const back = io.readText(join(admin, 'gitdir'))?.trim();
      if (!back || /[\x00-\x1f\x7f]/.test(back) || resolve(admin, back) !== join(holder, '.git')) {
        throw Error('unverified worktree backlink');
      }
    },
  };
  return io;
}

/** Bounded HEAD evidence for first-reflect deferral, never a prerequisite for routing. */
export function hasScopedHead(io: GitProbeIO, admin: string, common: string): boolean {
  return readScopedHead(io, admin, common) !== null;
}

export function readScopedHead(io: GitProbeIO, admin: string, common: string): string | null {
  const oid = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/;
  const valid = (value: string) => oid.test(value) && !/^0+$/.test(value);
  try {
    const head = io.readText(join(admin, 'HEAD'))?.trim();
    if (!head) return null;
    if (valid(head)) return head;
    const ref = /^ref: (refs\/[A-Za-z0-9_/-][A-Za-z0-9_.\/-]*)$/.exec(head)?.[1];
    if (!ref || ref.length > 512 || ref.split('/').some((part) => !part || part.startsWith('.') || part.endsWith('.lock'))) return null;
    const root = /^refs\/(bisect|worktree|rewritten)\//.test(ref) ? admin : common;
    const loose = io.readText(join(root, ref));
    if (loose !== null) return valid(loose.trim()) ? loose.trim() : null;
    const packed = io.readText(join(root, 'packed-refs'));
    for (const line of packed?.split('\n') ?? []) {
      const [name, path] = line.trim().split(' ', 2);
      if (path === ref && valid(name)) return name;
    }
  } catch { return null; }
  return null;
}
