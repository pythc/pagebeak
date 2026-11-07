// Small host adapter: external WorkBuddy writing, upstream Wiki storage/index/graph.
// No changes to upstream source; all endpoints are private and authenticated here.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { timingSafeEqual } from 'node:crypto';
import { join } from 'node:path';
import { existsSync, readdirSync, lstatSync } from 'node:fs';

const root = process.env.TENCENT_MEMORY_UPSTREAM!;
const require = createRequire(join(root, 'MemoryKnowledge/package.json'));
const { Hono } = require('hono');
const { serve } = require('@hono/node-server');
const { createApp } = await import(pathToFileURL(join(root, 'MemoryKnowledge/src/server.ts')).href);
const { app: upstream, config, knowledgeModule: km } = createApp();
const app = new Hono();
const key = process.env.KNOWLEDGE_SERVICE_KEY || '';
if (key.length < 32) throw new Error('A private service key is required');
app.use('*', async (c: any, next: any) => {
  if (c.req.path === '/health') return next();
  const supplied = Buffer.from(c.req.header('authorization') || '');
  const expected = Buffer.from('Bearer ' + key);
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected))
    return c.json({code: 401, message: 'Unauthorized'}, 401);
  return next();
});
app.post('/v3/zhixu/publish', async (c: any) => {
  const body = await c.req.json();
  const service = c.req.header('x-tdai-service-id');
  const row = service && typeof body.wiki_id === 'string' ? km.wikiService.getById(service, body.wiki_id) : null;
  if (!row || row.team_id !== body.team_id) return c.json({code: 404, message: 'Wiki not found'}, 404);
  if (!Array.isArray(body.pages) || body.pages.length > 20 || !body.pages.length ||
      !body.pages.every((p: any) => typeof p.ref === 'string' && typeof p.content === 'string'))
    return c.json({code: 400, message: 'Invalid pages'}, 400);
  const written = km.wikiService.pageWriteMany(service, row.team_id, row.wiki_id, body.pages);
  if (!Array.isArray(written)) return c.json({code: 409, message: 'Publish rejected'}, 409);
  km.wikiMgr.register({name: row.wiki_id, path: km.wikiService.dirFor(service, row.team_id, row.wiki_id)});
  const state = km.wikiMgr.sync(row.wiki_id);
  if (state.status !== 'ready') return c.json({code: 503, message: 'Index not ready'}, 503);
  km.store.updateWikiStatus(service, row.wiki_id, {
    status: 'ready', internal_status: null, sync_error: null, page_count: state.pageCount,
    version: Number(row.version) + 1, last_sync_at: new Date().toISOString(),
  });
  return c.json({code: 0, data: {items: written, page_count: state.pageCount, writer: 'external-agent'}});
});
// Upstream page/rm treats a missing file as traversal. Source deletion can already
// have removed it, so make cleanup retryable without weakening path validation.
app.post('/v3/zhixu/forget', async (c: any) => {
  const body = await c.req.json(), service = c.req.header('x-tdai-service-id');
  const row = service && typeof body.wiki_id === 'string' ? km.wikiService.getById(service, body.wiki_id) : null;
  if (!row || row.team_id !== body.team_id) return c.json({code: 404}, 404);
  if (!Array.isArray(body.refs) || body.refs.length > 20 || !body.refs.every((ref: unknown) =>
      typeof ref === 'string' && /^zhixu-[a-f0-9-]{36}-[0-9]+-[a-f0-9]{12}\.md$/.test(ref)))
    return c.json({code: 400}, 400);
  const dir = km.wikiService.dirFor(service, row.team_id, row.wiki_id);
  const present = body.refs.filter((ref: string) => existsSync(join(dir, 'wiki', ref)));
  if (present.length) await km.wikiService.pageRm(service, row.team_id, row.wiki_id, present);
  return c.json({code: 0, data: {removed: body.refs}});
});
// Inventory includes unpublished revisions and raw exports, not just indexed pages.
app.post('/v3/zhixu/inventory', async (c: any) => {
  const body = await c.req.json(), service = c.req.header('x-tdai-service-id');
  const row = service && typeof body.wiki_id === 'string' ? km.wikiService.getById(service, body.wiki_id) : null;
  if (!row || row.team_id !== body.team_id) return c.json({code: 404}, 404);
  const dir = km.wikiService.dirFor(service, row.team_id, row.wiki_id);
  const scan = (folder: string, pattern: RegExp) => existsSync(folder) ? readdirSync(folder)
    .filter((name: string) => pattern.test(name)).map((ref: string) => ({ref, stat: lstatSync(join(folder, ref))}))
    .filter((v: any) => v.stat.isFile() && !v.stat.isSymbolicLink())
    .map((v: any) => ({ref: v.ref, bytes: v.stat.size})) : [];
  const pages = scan(join(dir, 'wiki'), /^zhixu-[a-f0-9-]{36}-[0-9]+-[a-f0-9]{12}\.md$/);
  const raw = scan(join(dir, 'raw', 'sources'), /^[a-f0-9-]{36}-(?:[a-f0-9]{12}-)?[0-9]{4,}\.md$/);
  return c.json({code: 0, data: {pages, raw, bytes: [...pages, ...raw].reduce((n: number, v: any) => n + v.bytes, 0)}});
});
app.post('/v3/zhixu/literal', async (c: any) => {
  const body = await c.req.json(), service = c.req.header('x-tdai-service-id');
  const row = service && typeof body.wiki_id === 'string' ? km.wikiService.getById(service, body.wiki_id) : null;
  if (!row || row.team_id !== body.team_id) return c.json({code: 404}, 404);
  if (typeof body.query !== 'string' || !body.query.trim() || body.query.length > 200 || !Array.isArray(body.refs) ||
    body.refs.length > 100 || !body.refs.every((v: any) => typeof v === 'string' && /^zhixu-[a-f0-9-]{36}-[0-9]+-[a-f0-9]{12}\.md$/.test(v)))
    return c.json({code: 400}, 400);
  const items = [];
  for (const ref of body.refs) {
    const content = km.wikiService.pageRead(service, row.team_id, row.wiki_id, ref);
    if (content === null) continue;
    const start = content.normalize('NFC').toLocaleLowerCase().indexOf(body.query.trim().normalize('NFC').toLocaleLowerCase());
    if (start >= 0) items.push({ref, snippet: content.slice(Math.max(0, start - 60), start + 200)});
  }
  return c.json({code: 0, data: {items}});
});
// Deliberately do not expose ingest: this deployment has no background model API.
app.post('/v3/wiki/ingest', (c: any) => c.json({code: 409, message: 'Use WorkBuddy external-agent publishing'}, 409));
app.route('/', upstream);
const server = serve({fetch: app.fetch, hostname: '127.0.0.1', port: config.port});
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => server.close(() => process.exit(0)));
