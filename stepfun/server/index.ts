import express from 'express';
import { createServer as createViteServer } from 'vite';
import { z, ZodError } from 'zod';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { loadEnvFile } from 'node:process';
import { Database, HttpError } from './database';
import { MODELS } from '../shared/model';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
if (existsSync(resolve(root, '.env'))) loadEnvFile(resolve(root, '.env'));
const app = express();
const db = new Database();
app.disable('x-powered-by');
app.use((_req, res, next) => {
  res.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
  res.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://127.0.0.1:*; img-src 'self' data:; worker-src 'self' blob:; font-src 'self'; object-src 'none'");
  next();
});
app.use('/models', express.static(resolve(root, 'public/models'), { fallthrough: false }));
app.use('/ort', express.static(resolve(root, 'public/ort'), { fallthrough: false }));
// Loopback application. Reject cross-origin writes; no permissive CORS.
app.use('/api', (req, _res, next) => {
  if (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`) return next(new HttpError(403, 'Cross-origin API request rejected'));
  next();
}, express.json({ limit: '12mb' }));

const idSchema = z.string().regex(/^kb_[a-f0-9]{32}$/);
const vector = z.array(z.number().finite()).min(1).max(1024);
const chunk = z.object({ id: z.string().min(1).max(100), text: z.string().trim().min(1).max(12000),
  source: z.string().max(500), title: z.string().max(200), part: z.number().int().nonnegative(), embedding: vector });

app.get('/api/health', async (_req, res) => {
  await db.ready();
  res.json({ ok: true, database: db.database, version: db.version, inference: 'browser-only', models: MODELS });
});
app.get('/api/collections', async (_req, res) => res.json(await db.list()));
app.post('/api/collections', async (req, res) => {
  const body = z.object({ name: z.string().trim().min(1).max(100), model: z.enum(['gemma2', 'bge']) }).parse(req.body);
  res.status(201).json(await db.create(body.name, body.model));
});
app.delete('/api/collections/:id', async (req, res) => { await db.remove(idSchema.parse(req.params.id)); res.json({ ok: true }); });
app.post('/api/collections/:id/chunks', async (req, res) => {
  const body = z.object({ modelKey: z.string(), chunks: z.array(chunk).min(1).max(64), refresh: z.boolean().default(false) }).parse(req.body);
  res.json(await db.write(idSchema.parse(req.params.id), body.modelKey, body.chunks, body.refresh));
});
app.post('/api/collections/:id/refresh', async (req, res) => res.json(await db.refresh(idSchema.parse(req.params.id))));
app.post('/api/collections/:id/search', async (req, res) => {
  const body = z.object({ modelKey: z.string(), query: z.string().trim().min(1).max(2000), embedding: vector,
    k: z.number().int().min(1).max(50).default(5), mode: z.enum(['hybrid', 'vector']).default('hybrid') }).parse(req.body);
  res.json(await db.search(idSchema.parse(req.params.id), body.modelKey, body.query, body.embedding, body.k, body.mode));
});
app.post('/api/benchmarks', async (req, res) => {
  const report = z.object({ schemaVersion: z.literal(1), profiles: z.array(z.unknown()).max(10) }).passthrough().parse(req.body);
  const filename = `benchmark-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
  await mkdir(resolve(root, 'artifacts'), { recursive: true });
  await writeFile(resolve(root, 'artifacts', filename), JSON.stringify(report, null, 2), { flag: 'wx' });
  res.json({ path: `artifacts/${filename}` });
});
app.use('/api', (_req, res) => res.status(404).json({ error: 'API route not found' }));
app.use((error: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error(error);
  const status = error instanceof HttpError ? error.status : error instanceof ZodError ? 400 : 503;
  res.status(status).json({ error: error instanceof ZodError ? error.issues.map(i => `${i.path.join('.')}: ${i.message}`).join('; ') : error instanceof Error ? error.message : 'Unknown error' });
});

if (process.argv.includes('--production')) {
  app.use(express.static(resolve(root, 'dist')));
  app.get('/{*path}', (_req, res) => res.sendFile(resolve(root, 'dist/index.html')));
} else {
  const vite = await createViteServer({ root, server: { middlewareMode: true }, appType: 'spa' });
  app.use(vite.middlewares);
}
const server = app.listen(Number(process.env.PORT ?? 4177), '127.0.0.1', () => console.log(`Edge Atlas ready at http://127.0.0.1:${process.env.PORT ?? 4177}`));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => { void db.close().finally(() => process.exit(0)); }));
