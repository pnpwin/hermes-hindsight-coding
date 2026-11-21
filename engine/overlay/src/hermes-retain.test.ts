import { mkdtempSync, readFileSync, realpathSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Config } from './core/config';
import { HindsightClient } from './core/hindsight';
import { retainSnapshot } from './hermes-retain';

let root: string;
const bank = 'coding-agent::fixture';
const id = 'hermes-' + 'a'.repeat(64);
const documentId = `conversation:${id}`;
const policy = () => true;
const turns = [
  {role: 'user', content: 'fixture ask', timestamp: '2026-04-05T00:00:00.000Z'},
  {role: 'assistant', content: 'fixture answer', timestamp: '2026-04-05T00:00:01.000Z'},
];
const cfg = { retainSessions: true, repositorySnapshot: { status: 'absent' } } as Config;
function fake(conversation: boolean) {
  const client = new HindsightClient({ apiUrl: 'http://127.0.0.1:1', bank });
  vi.spyOn(client, 'req').mockImplementation(async (_method, url) => Response.json(url.endsWith('/config')
    ? { bank_id: bank, config: { retain_strategies: conversation ? { conversation: {} } : {} } }
    : { status: 'completed' }));
  vi.spyOn(client, 'supportsAppendRetain').mockResolvedValue(true);
  vi.spyOn(client, 'supportsIdempotentRetain').mockResolvedValue(true);
  const submit = vi.spyOn(client, 'retain').mockResolvedValue(undefined);
  return { client, submit };
}

beforeEach(() => { root = realpathSync(mkdtempSync(join(tmpdir(), 'hermes-retain-'))); });
afterEach(() => { rmSync(root, { recursive: true, force: true }); vi.restoreAllMocks(); });

describe('Hermes retain via upstream cursor', () => {
  it('requires an explicit existing conversation strategy without altering bank config', async () => {
    const { client, submit } = fake(false);
    await expect(retainSnapshot({ client, cfg, engineId: id, cursorFile: join(root, 'cursor.json'),
      cwd: root, startCwd: root, turns, rewound: false, authorized: policy })).rejects.toThrow();
    expect(submit).not.toHaveBeenCalled();
  });

  it('persists the cursor and reuses upstream REF-ID, stamp, append and duplicate idempotency', async () => {
    const { client, submit } = fake(true);
    const cursorFile = join(root, 'cursor.json');
    const input = { client, cfg, engineId: id, cursorFile, cwd: root, startCwd: root,
      turns, rewound: false, authorized: policy };
    await retainSnapshot(input);
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit.mock.calls[0][2]).toBe(documentId);
    expect(submit.mock.calls[0][3]).toEqual(expect.arrayContaining(['source:chat', 'harness:hermes']));
    expect(submit.mock.calls[0][4]).toBe('conversation');
    expect(JSON.parse(submit.mock.calls[0][0].split('\n')[0]).content).toBe(`REF-ID: ${documentId}`);
    expect(statSync(cursorFile).mode & 0o777).toBe(0o600);
    await retainSnapshot(input);
    expect(submit).toHaveBeenCalledTimes(1);
    const next = [...turns, ...turns.map((row) => ({...row, timestamp: row.timestamp.replace('00:00:', '00:01:')}))];
    await retainSnapshot({ ...input, turns: next });
    expect(submit).toHaveBeenCalledTimes(2);
    expect(submit.mock.calls[1][5]?.updateMode).toBe('append');
    expect(submit.mock.calls[1][0].split('\n').map((line) => JSON.parse(line))).toEqual(next.slice(2));
    expect(JSON.parse(readFileSync(cursorFile, 'utf8')).turns).toBe(4);
  });
});
