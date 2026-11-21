import { HindsightClient } from './core/hindsight';
import type { Config } from './core/config';

const MAX_RESPONSE_BYTES = 512 * 1024;
export class SeedNotSubmitted extends Error {}
export const PAGE_ID = /^kp-[A-Za-z0-9_-]{1,189}$/;
const GET = /^(?:\/(?:documents|operations)|\/(?:pages|knowledge-base)\/(?:tree|search)|\/(?:pages|knowledge-base\/pages)\/kp-[A-Za-z0-9_-]{1,189})$/;

export function readOnlyClient(cfg: Config, bank: string, project?: string) {
  return scopedClient(cfg, bank, project);
}

export function scopedClient(cfg: Config, bank: string, project?: string, write?: {
  documentId: string; authorized: () => boolean; strategy?: 'gitlog'; beforeSeedPost?: () => boolean;
}) {
  const origin = new URL(cfg.apiUrl).origin;
  const prefix = `/v1/default/banks/${encodeURIComponent(bank)}`;
  const guardedFetch: typeof fetch = async (input, init) => {
    if (typeof input !== 'string') throw Error('invalid internal request');
    const target = new URL(input);
    const path = target.pathname.startsWith(prefix + '/') ? target.pathname.slice(prefix.length) : '';
    const mayRetain = write && init?.method === 'POST' && path === '/memories' && !target.search &&
      typeof init.body === 'string' && write.authorized();
    const readRetainState = write && init?.method === 'GET' && !target.search &&
      (path === '/config' || /^\/operations\/[a-f0-9-]{36}$/.test(path) || ['/v1/version', '/version'].includes(target.pathname) ||
        (write.strategy === 'gitlog' && path === `/documents/${encodeURIComponent(write.documentId)}`));
    if (target.origin !== origin || target.hash || target.username || target.password ||
        !(mayRetain || readRetainState || (!write && ((init?.method === 'GET' && GET.test(path)) ||
          (init?.method === 'POST' && (path === '/reflect' || path === '/memories/recall')))))) {
      throw Error('request not allowed');
    }
    let expectedOp: string | undefined;
    if (mayRetain) {
      const body = JSON.parse(init!.body as string);
      if (body.async !== true || !Array.isArray(body.items) || body.items.length !== 1 ||
          body.items[0].document_id !== write!.documentId || body.items[0].strategy !== (write!.strategy ?? 'conversation') ||
          (write!.strategy === 'gitlog' && body.items[0].update_mode !== undefined) ||
          typeof body.operation_id !== 'string' || !/^[a-f0-9-]{36}$/.test(body.operation_id)) throw Error('invalid retain scope');
      expectedOp = body.operation_id;
    }
    if (expectedOp && write?.strategy === 'gitlog' && !write.beforeSeedPost?.()) throw new SeedNotSubmitted('seed lease revoked');
    const response = await fetch(input, { ...init, redirect: 'error' });
    // A validated refusal differs from an ambiguous transport failure or lost receipt.
    if (expectedOp && write?.strategy === 'gitlog' && [400, 401, 403, 422, 429].includes(response.status)) {
      void response.body?.cancel().catch(() => {});
      throw new SeedNotSubmitted('seed request rejected');
    }
    if (!response.body) {
      if (expectedOp && response.ok) throw Error('missing operation receipt');
      return response;
    }
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let length = 0;
    try {
      for (;;) {
        const next = await reader.read();
        if (next.done) break;
        length += next.value.byteLength;
        if (length > MAX_RESPONSE_BYTES) throw Error('response limit exceeded');
        chunks.push(next.value);
      }
    } catch (error) {
      void reader.cancel().catch(() => {});
      throw error;
    } finally { reader.releaseLock(); }
    const bytes = Buffer.concat(chunks, length);
    if (expectedOp && (response.status === 404 || (response.ok && JSON.parse(bytes.toString('utf8')).operation_id !== expectedOp))) {
      throw Error('invalid operation receipt');
    }
    return new Response(bytes, { status: response.status, headers: response.headers });
  };
  return new HindsightClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank, project,
    observationScopes: cfg.observationScopes, pageSearchLimit: cfg.pageSearchLimit,
    recallOptions: cfg.recallOptions, requestTimeoutMs: write ? 3300 : 1200, fetchOverride: guardedFetch, strictReadStatus: true });
}

// Untrusted response data never supplies opening/closing tags or invisible role markers.
export function safeText(text: string, limit: number): string {
  return text.slice(0, limit).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}
