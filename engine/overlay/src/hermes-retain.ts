import { constants, openSync, fstatSync, closeSync, fsyncSync, readSync, writeFileSync, unlinkSync, renameSync, realpathSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { dirname } from 'node:path';
import { retainLiveSession, type TransportTurn } from './core/chat';
import type { Config } from './core/config';
import type { HindsightClient } from './core/hindsight';
import { fingerprintTurns, type RetainCursor, type RetainCursorStore } from './core/retain-cursor';
import { buildRetainStamp } from './core/retain-stamp';

const MAX_CURSOR = 1024 * 1024;
const MAX_TRANSCRIPT = 512 * 1024;
const ID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/;
const ACTION = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;
const DATA_MARKER = /(?:^|[^a-z0-9_])data:/i;
const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);

function safeTurn(v: unknown): v is TransportTurn {
  return isRecord(v) && Object.keys(v).length === 3 && typeof v.content === 'string' && !!v.content.trim() &&
    !DATA_MARKER.test(v.content) && typeof v.timestamp === 'string' && ISO.test(v.timestamp) &&
    Number.isFinite(Date.parse(v.timestamp)) && new Date(v.timestamp).toISOString() === v.timestamp &&
    (v.role === 'user' || v.role === 'assistant' || (v.role === 'action' && ACTION.test(v.content)));
}

export function validTurns(value: unknown): value is TransportTurn[] {
  return Array.isArray(value) && value.length > 1 && value.length <= 4096 && value[0]?.role === 'user' &&
    value.at(-1)?.role === 'assistant' && value.every(safeTurn) &&
    Buffer.byteLength(JSON.stringify(value)) <= MAX_TRANSCRIPT;
}

function safePending(content: unknown): boolean {
  if (typeof content !== 'string' || Buffer.byteLength(content) > MAX_TRANSCRIPT) return false;
  const lines = content.split('\n');
  if (lines.length > 4096) return false;
  try {
    return lines.every((line) => {
      const turn: unknown = JSON.parse(line);
      // Coding Agents emits JSON.stringify at claim time, then replays the original exact bytes.
      // Validate that exact representation: JSON.parse alone hides duplicate/escaped keys.
      // Reject rather than reserialize, since changing bytes would break operation-ID idempotency.
      return safeTurn(turn) && JSON.stringify(turn) === line;
    });
  } catch { return false; }
}

function privateCursor(path: string, key: string, bank: string): RetainCursorStore {
  const directory = dirname(path);
  if (realpathSync(directory) !== directory) throw Error('unsafe cursor directory');
  return {
    read(sessionId) {
      if (sessionId !== key) throw Error('wrong cursor id');
      let fd: number;
      try { fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
      catch (err) { if ((err as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw err; }
      let value: RetainCursor;
      try {
        const st = fstatSync(fd);
        if (!st.isFile() || st.nlink !== 1 || st.uid !== process.getuid?.() || (st.mode & 0o777) !== 0o600 ||
            st.size < 1 || st.size > MAX_CURSOR) throw Error('unsafe cursor');
        const buffer = Buffer.alloc(st.size);
        let read = 0;
        while (read < buffer.length) {
          const n = readSync(fd, buffer, read, buffer.length - read, null);
          if (n === 0) throw Error('truncated cursor');
          read += n;
        }
        if (fstatSync(fd).size !== st.size) throw Error('changed cursor');
        value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer));
      } finally { closeSync(fd); }
      if (!value || value.bank !== bank || !Number.isSafeInteger(value.turns) || value.turns <= 0 ||
          value.turns > 4096 || !/^[a-f0-9]{40}$/.test(value.fingerprint) || value.paths || value.segments ||
          (value.replaceClaim !== undefined && (value.dirty !== true || !ID.test(value.replaceClaim))) ||
          (value.ops !== undefined && (!Array.isArray(value.ops) || !value.ops.every((id) => typeof id === 'string' && ID.test(id)))) ||
          (value.pending !== undefined && (!Array.isArray(value.pending) || !value.pending.every((p) => p &&
            safePending(p.content) && ID.test(p.operationId) && Number.isSafeInteger(p.at))))) throw Error('invalid cursor');
      return value;
    },
    clear(sessionId) {
      if (sessionId !== key) throw Error('wrong cursor id');
      unlinkSync(path);
      const dir = openSync(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(dir); } finally { closeSync(dir); }
    },
    write(sessionId, cursor) {
      if (sessionId !== key || cursor.bank !== bank) throw Error('wrong cursor scope');
      const body = JSON.stringify(cursor);
      if (Buffer.byteLength(body) > MAX_CURSOR) throw Error('cursor limit');
      const tmp = `${path}.${randomBytes(16).toString('hex')}.tmp`;
      const fd = openSync(tmp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
      try {
        try { writeFileSync(fd, body); fsyncSync(fd); } finally { closeSync(fd); }
        renameSync(tmp, path);
        const dir = openSync(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        try { fsyncSync(dir); } finally { closeSync(dir); }
      } finally {
        try { unlinkSync(tmp); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
      }
    },
  };
}

export async function retainSnapshot(args: {
  client: HindsightClient; cfg: Config; engineId: string; cursorFile: string; cwd: string; startCwd: string;
  turns: unknown; rewound: boolean; authorized: () => boolean; project?: string; retryUntil?: number;
  allowBankDefaults?: () => boolean;
}) {
  const { cfg, client, engineId, turns } = args;
  if (!args.authorized() || cfg.retainSessions !== true || !/^hermes-[a-f0-9]{64}$/.test(engineId) || !validTurns(turns)) {
    throw Error('retain unavailable');
  }
  const store = privateCursor(args.cursorFile, engineId, client.bank);
  const prior = store.read(engineId);
  if (prior && !args.rewound && (prior.turns > turns.length || fingerprintTurns(turns, prior.turns) !== prior.fingerprint)) {
    throw Error('unproven transcript prefix');
  }
  const response = await client.req('GET', client.bankUrl('/config'));
  if (!response.ok) throw Error('bank config unavailable');
  const body: unknown = await response.json();
  if (!isRecord(body) || body.bank_id !== client.bank || !isRecord(body.config)) {
    throw Error('bank config invalid');
  }
  const strategies = body.config.retain_strategies;
  if (strategies != null && !isRecord(strategies)) throw Error('bank strategies invalid');
  if (isRecord(strategies) && 'conversation' in strategies && !isRecord(strategies.conversation)) {
    throw Error('conversation strategy unavailable');
  }
  if (!isRecord(strategies) || !('conversation' in strategies)) {
    if (!args.allowBankDefaults?.()) throw Error('conversation strategy unavailable');
    // Installations differ. Only the verified 1.2.1 runtime falls back to the resolved
    // bank configuration for an unknown strategy; the wire still names `conversation`.
    const version = await client.req('GET', `${cfg.apiUrl}/version`);
    const proof: unknown = await version.json();
    if (!version.ok || !isRecord(proof) || proof.api_version !== '1.2.1') throw Error('strategy fallback unverified');
  }
  if (!args.authorized()) throw Error('retain revoked');
  if (!(await client.supportsIdempotentRetain())) throw Error('idempotent retain unavailable');
  const startTs = turns[0].timestamp!;
  const stamp = buildRetainStamp(cfg, { directory: args.cwd, sessionRoot: args.startCwd,
    harness: 'hermes', bankId: client.bank, sessionId: engineId, gitProject: args.project ?? 'unknown', timestamp: startTs });
  await retainLiveSession(client, engineId, turns, startTs, 'hermes', {
    cursors: store, stamp, retryUntil: args.retryUntil ?? Date.now() + 17000,
    requestBudgetMs: 3300, stableReplaceReceipt: true,
  });
  const current = store.read(engineId);
  if (!current || current.pending?.length || current.dirty || current.turns !== turns.length ||
      current.fingerprint !== fingerprintTurns(turns, turns.length)) throw Error('retain replay deferred');
}
