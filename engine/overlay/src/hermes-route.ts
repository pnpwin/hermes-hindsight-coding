import { realpathSync, statSync } from 'node:fs';
import { isAbsolute, parse } from 'node:path';
import { resolveProjectRoot } from './core/bank';
import type { RawConfig } from './core/config';
import { resolveHostConfig } from './core/host-client';
import { assertDirectoryPin, directoryPin, hasScopedHead, scopedGitIO, within, type DirectoryPin } from './hermes-git-io';

export interface RouteInput {
  cwd: string;
  startCwd: string;
  rootPins: DirectoryPin[];
  routingHome: string;
  rawConfig: RawConfig;
}

function physicalDirectory(path: string) {
  if (!isAbsolute(path)) throw new Error('absolute directory required');
  const real = realpathSync(path);
  if (!statSync(real).isDirectory()) throw new Error('directory required');
  return real;
}


function sameFile(path: string, other: string) {
  const a = statSync(path, { bigint: true });
  const b = statSync(other, { bigint: true });
  return a.dev === b.dev && a.ino === b.ino;
}

/** Filesystem scope is a stricter outer gate; the upstream engine names and configures the bank. */
export function resolveHermesRoute(args: RouteInput) {
  try {
    const cwd = physicalDirectory(args.cwd);
    const startCwd = physicalDirectory(args.startCwd);
    const routingHome = physicalDirectory(args.routingHome);
    args.rootPins.forEach(assertDirectoryPin);
    if (args.rootPins.some(({path}) => sameFile(path, parse(path).root) ||
        (path !== routingHome && sameFile(path, routingHome)))) return null;
    const atHome = sameFile(cwd, routingHome) || sameFile(startCwd, routingHome);
    if (atHome && (cwd !== routingHome || startCwd !== routingHome ||
        !args.rootPins.some((pin) => pin.path === routingHome))) return null;
    const projectPins = args.rootPins.filter((pin) => pin.path !== routingHome);
    const roots = projectPins.map((pin) => pin.path);
    if (!atHome && (!within(cwd, roots) || !within(startCwd, roots))) return null;
    const cwdPin = directoryPin(cwd);
    const startPin = cwd === startCwd ? cwdPin : directoryPin(startCwd);
    // HOME is an exact non-Git session, never authority to read a repository there.
    const io = scopedGitIO(atHome ? [] : projectPins, [cwd, startCwd]);
    let admin: { path: string; common: string } | undefined;
    const validateMarker = io.validateMarker;
    io.validateMarker = (holder, gitDir, layout, pointer) => {
      validateMarker?.(holder, gitDir, layout, pointer);
      admin = { path: gitDir, common: layout.commonDir };
    };
    const repo = resolveProjectRoot(cwd, io);
    const currentAdmin = admin;
    const startRepo = cwd === startCwd ? repo : resolveProjectRoot(startCwd, io);
    if (repo.status === 'failed' || startRepo.status === 'failed') return null;
    const projectRoot = repo.status === 'resolved' ? physicalDirectory(repo.root) : null;
    const startRoot = startRepo.status === 'resolved' ? physicalDirectory(startRepo.root) : null;
    if (projectRoot !== startRoot || (projectRoot && !within(projectRoot, roots))) return null;
    if (!atHome && !projectRoot && !roots.some((root) => within(cwd, [root]) && within(startCwd, [root]) && within(cwd, [startCwd]))) return null;
    const projectIdentity = projectRoot ? directoryPin(projectRoot) : null;
    const resolved = resolveHostConfig('hermes', cwd, {
      // The same physical path must drive approval, explicit mapping, and bank derivation.
      sessionRoot: startCwd, rawConfig: args.rawConfig, routingHome,
      repositorySnapshot: repo,
    });
    if (resolved.cfg.disabled || !resolved.bankId) return null;
    args.rootPins.forEach(assertDirectoryPin);
    assertDirectoryPin(cwdPin);
    assertDirectoryPin(startPin);
    if (physicalDirectory(args.cwd) !== cwd || physicalDirectory(args.startCwd) !== startCwd) return null;
    if (projectIdentity) assertDirectoryPin(projectIdentity);
    const hasGitHistory = () => projectRoot !== null &&
      hasScopedHead(io, currentAdmin?.path ?? projectRoot, currentAdmin?.common ?? projectRoot);
    return { ...resolved, cwd, startCwd, projectRoot, projectIdentity, hasGitHistory };
  } catch {
    return null;
  }
}
