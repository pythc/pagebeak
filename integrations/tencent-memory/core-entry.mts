// Native Tencent MemoryCore data plane hosted for an external WorkBuddy executor.
// No capture mirror, extraction scheduler, credential discovery or model API.
import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const root = join(process.env.TENCENT_MEMORY_UPSTREAM!, 'MemoryCore');
const load = (p: string) => import(pathToFileURL(join(root, p)).href);
const { parseConfig } = await load('src/config.ts');
const { StorePool } = await load('src/core/store/store-pool.ts');
const { handleV2Route } = await load('src/gateway/v2-router.ts');
const logger = {debug: () => {}, info: () => {}, warn: () => console.warn('Tencent Core warning'),
  error: () => console.error('Tencent Core operation failed')};
const pool = new StorePool({mode: 'sqlite', dataDir: process.env.TDAI_DATA_DIR,
  memoryCfg: parseConfig({extraction: {enabled: false}, embedding: {provider: 'none'},
    bm25: {enabled: true, language: 'zh'}, skill: {enabled: false}}),
  kafka: {enabled: false}, maxStores: 100, logger});
const key = process.env.TDAI_GATEWAY_API_KEY || '';
if (key.length < 32) throw new Error('A private service key is required');
const allowed = new Set(['add', 'query', 'search', 'delete'].map(p => '/v3/conversation/' + p));
const send = (res: any, status: number, body: unknown) => {
  res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body));
};
const server = createServer(async (req, res) => {
  if (req.url === '/health' && req.method === 'GET') return send(res, 200, {status: 'ok', executor: 'external-workbuddy'});
  const supplied = Buffer.from(req.headers.authorization || ''), expected = Buffer.from('Bearer ' + key);
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return send(res, 401, {code: 401});
  const service = req.headers['x-tdai-service-id'];
  if (typeof service !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(service)) return send(res, 400, {code: 400});
  if ((!allowed.has(req.url || '') && !['/v3/zhixu/inventory', '/v3/zhixu/literal'].includes(req.url || '')) || req.method !== 'POST') return send(res, 404, {code: 404});
  try {
    if (req.url?.startsWith('/v3/zhixu/')) {
      const chunks = []; let size = 0;
      for await (const chunk of req) {size += chunk.length; if (size > 512 * 1024) throw new Error('Body too large'); chunks.push(chunk);}
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (body.team_id !== service || body.user_id !== service || body.agent_id !== 'zhixu-workbuddy')
        return send(res, 400, {code: 400});
      const {store} = await pool.getStore(service);
      const scope = {teamId: service, userId: service, agentId: 'zhixu-workbuddy'};
      if (req.url.endsWith('/literal')) {
        if (typeof body.query !== 'string' || !body.query.trim() || body.query.length > 200 ||
          !Array.isArray(body.sessions) || body.sessions.length > 100 || !body.sessions.every((v: any) => typeof v === 'string'))
          return send(res, 400, {code: 400});
        const items = [];
        for (const sessionId of body.sessions) {
          const data = await store.queryL0Paginated({...scope, sessionId, limit: 100, offset: 0});
          if (data.total > 100) throw new Error('Memory session exceeds host bound');
          const parts = data.rows.map((r: any) => {try {return JSON.parse(r.message_text);} catch {return {part:0,content:''};}});
          const text = parts.sort((a: any,b: any)=>a.part-b.part).map((p: any)=>p.content || '').join('');
          const start = text.normalize('NFC').toLocaleLowerCase().indexOf(body.query.trim().normalize('NFC').toLocaleLowerCase());
          if (start >= 0) items.push({session_id: sessionId, snippet: text.slice(Math.max(0, start - 60), start + 200)});
        }
        return send(res, 200, {code: 0, data: {items}});
      }
      let offset = 0, total = 0, bytes = 0; const sessions = new Set<string>();
      do {
        const data = await store.queryL0Paginated({...scope, limit: 1000, offset}); total = data.total;
        if (total > 50000) throw new Error('Inventory limit exceeded');
        for (const r of data.rows) {sessions.add(r.session_id); bytes += Buffer.byteLength(r.message_text, 'utf8');}
        offset += data.rows.length;
        if (!data.rows.length) break;
      } while (offset < total);
      return send(res, 200, {code: 0, data: {sessions: [...sessions], bytes, messages: total}});
    }
    await handleV2Route(req, res, req.url, req.method, async (request: any) => {
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 512 * 1024) throw new Error('Body too large');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      // Scope is assigned by the authenticated application, not user content.
      if (body.team_id !== service || body.user_id !== service || body.agent_id !== 'zhixu-workbuddy')
        throw new Error('Scope mismatch');
      return body;
    }, send, {getStore: () => undefined, getEmbedding: () => undefined, getStorage: () => undefined,
      resolveStore: async (id: string) => pool.getStore(id), deployMode: 'standalone',
      v3StrictIsolation: true, logger});
  } catch {
    if (!res.headersSent) send(res, 500, {code: 500, message: 'Tencent operation failed'});
  }
});
server.listen(Number(process.env.TDAI_GATEWAY_PORT || '8420'), '127.0.0.1');
for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => {
  server.close(async () => { await pool.closeAll(); process.exit(0); });
});
