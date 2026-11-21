import { createHash, randomBytes } from 'node:crypto';
import { constants, openSync, closeSync, fsyncSync, fstatSync, readSync, writeFileSync, renameSync, unlinkSync } from 'node:fs';
import { dirname } from 'node:path';
import { retainGitMessageSnapshot } from './core/git';
import type { HindsightClient } from './core/hindsight';
import { uuidV5 } from './core/uuid';
import type { GitMessages } from './hermes-seed';
import { SeedNotSubmitted } from './hermes-read';

export const seedDigest = (s: GitMessages) => createHash('sha256')
  .update(JSON.stringify([s.repoName, s.head, s.timestamp, s.content])).digest('hex');

type Claim = { owner: string; head: string; digest: string; operationId: string };
const OP_ID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;

function readClaim(path: string): Claim | null {
  let fd: number;
  try { fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null; throw error; }
  try {
    const r = fstatSync(fd);
    if (!r.isFile() || r.nlink !== 1 || r.uid !== process.getuid?.() || (r.mode & 0o777) !== 0o600 || r.size > 512) throw Error('unsafe seed state');
    const data = Buffer.alloc(513);
    const n = readSync(fd, data, 0, data.length, 0);
    if (n !== r.size) throw Error('changed seed state');
    const raw = new TextDecoder('utf-8', { fatal: true }).decode(data.subarray(0, n));
    const body = JSON.parse(raw) as Claim;
    if (JSON.stringify({ owner: body.owner, head: body.head, digest: body.digest, operationId: body.operationId }) !== raw ||
        typeof body.owner !== 'string' || !/^[a-f0-9]{64}$/.test(body.owner) ||
        typeof body.head !== 'string' || !/^[a-f0-9]{40}$/.test(body.head) ||
        typeof body.digest !== 'string' || !/^[a-f0-9]{64}$/.test(body.digest) ||
        typeof body.operationId !== 'string' || !OP_ID.test(body.operationId)) throw Error('invalid seed state');
    return body;
  } finally { closeSync(fd); }
}

function saveClaim(path: string, value: Claim) {
  const scratch = `${path}.${randomBytes(16).toString('hex')}.tmp`;
  const fd = openSync(scratch, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(scratch, path);
    const dir = openSync(dirname(path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try { fsyncSync(dir); } finally { closeSync(dir); }
  } finally {
    try { unlinkSync(scratch); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
}

function removeClaim(path: string) {
  unlinkSync(path);
  const dir = openSync(dirname(path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { fsyncSync(dir); } finally { closeSync(dir); }
}

export async function seedOne(client: HindsightClient, snapshot: GitMessages, stateFile: string,
  owner: string, authorized: () => boolean): Promise<'accepted' | 'current' | 'deferred'> {
  if (!authorized()) throw Error('seed revoked');
  const doc = `gitlog:${snapshot.repoName}`;
  const marker = `hermes-seed-owner:${owner}`;
  const claim = readClaim(stateFile);
  if (claim && claim.owner !== owner) throw Error('seed state owned elsewhere');
  const document = await client.req('GET', client.bankUrl(`/documents/${encodeURIComponent(doc)}`));
  if (document.ok) {
    const existing = (await document.json()) as { id?: string; tags?: unknown };
    if (existing.id !== doc || !Array.isArray(existing.tags) || !existing.tags.includes(marker)) throw Error('gitlog owned elsewhere');
    if (existing.tags.includes(`gitlog-head:${snapshot.head}`)) return 'current';
  } else if (document.status !== 404) throw Error('document state unknown');
  const digest = seedDigest(snapshot);
  let reused: string | undefined;
  if (claim) {
    const result = await client.req('GET', client.bankUrl(`/operations/${claim.operationId}`));
    if (!result.ok && result.status !== 404) throw Error('unknown operation');
    let status: string;
    if (result.status === 404) status = 'missing';
    else {
      const reply = await result.json() as { operation_id?: string; status?: string } | null;
      if (reply?.operation_id !== claim.operationId || typeof reply.status !== 'string') throw Error('unknown operation');
      status = reply.status === 'not_found' ? 'missing' : reply.status;
    }
    if (status === 'missing' && claim.digest === digest) reused = claim.operationId;
    else if (status === 'completed' && claim.head === snapshot.head) return 'current';
    else if (!['completed', 'failed', 'cancelled'].includes(status ?? '')) return 'deferred';
  }
  const response = await client.req('GET', client.bankUrl('/config'));
  if (!response.ok) throw Error('seed strategy unavailable');
  const cfg = await response.json() as { bank_id?: string; config?: { retain_strategies?: { gitlog?: unknown } } };
  const strategy = cfg.config?.retain_strategies?.gitlog;
  if (cfg.bank_id !== client.bank || !strategy || typeof strategy !== 'object' || Array.isArray(strategy) ||
      !(await client.supportsIdempotentRetain()) || !authorized()) throw Error('seed strategy unavailable');
  const operationId = reused ?? uuidV5(JSON.stringify([client.bank, doc, marker, digest, claim?.operationId]));
  saveClaim(stateFile, { owner, head: snapshot.head, digest, operationId });
  try {
    await retainGitMessageSnapshot(client, { ...snapshot, operationId }, { tags: [marker, 'harness:hermes'], metadata: { harness: 'hermes' } });
  } catch (error) {
    // A replay may be refused even after its original submission reached the server.
    if (!reused && error instanceof SeedNotSubmitted) {
      if (claim) saveClaim(stateFile, claim);
      else removeClaim(stateFile);
    }
    throw error;
  }
  return 'accepted';
}
