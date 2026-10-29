import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { SeekdbClient, SeekdbAdminClient, Schema, FulltextIndexConfig, VectorIndexConfig } from 'seekdb';
import { MODELS, modelKey, type ModelId } from '../shared/model';

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

// The SDK hybridSearch uses a session variable across multiple SQL statements.
// Serialize complete operations, including reads, on this ONE client connection.
export class SerialQueue {
  private tail: Promise<unknown> = Promise.resolve();
  run<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.tail.then(fn, fn);
    this.tail = next.catch(() => undefined);
    return next;
  }
}

export type Chunk = { id: string; text: string; source: string; title: string; part: number; embedding: number[] };
export type KnowledgeBase = { id: string; name: string; model: ModelId; modelKey: string; count: number; revision: number; indexedRevision: number; createdAt: string };

export class Database {
  private client!: SeekdbClient;
  private queue = new SerialQueue();
  private initialization?: Promise<void>;
  version = '';
  readonly database = process.env.SEEKDB_DATABASE ?? 'stepfun_edge_kb';

  async ready() {
    if (!this.initialization) this.initialization = this.init().catch(error => { this.initialization = undefined; throw error; });
    await this.initialization;
  }

  private async init() {
    if (!/^[a-zA-Z][a-zA-Z0-9_]{0,60}$/.test(this.database)) throw new Error('Invalid SEEKDB_DATABASE');
    const connection = process.env.SEEKDB_PATH ? { path: process.env.SEEKDB_PATH } : {
      host: process.env.SEEKDB_HOST ?? '127.0.0.1', port: Number(process.env.SEEKDB_PORT ?? 2889),
      user: process.env.SEEKDB_USER ?? 'root', password: process.env.SEEKDB_PASSWORD ?? '',
    };
    const admin = new SeekdbAdminClient(connection);
    try { await admin.createDatabase(this.database); } finally { await admin.close(); }
    this.client = new SeekdbClient({ ...connection, database: this.database, queryTimeout: 60000 });
    await this.client.execute(`CREATE TABLE IF NOT EXISTS edge_kb_registry (
      id VARCHAR(64) PRIMARY KEY, name VARCHAR(120) NOT NULL, model VARCHAR(20) NOT NULL,
      model_key VARCHAR(255) NOT NULL, revision BIGINT NOT NULL DEFAULT 0,
      indexed_revision BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )`);
    const rows = await this.client.execute('SELECT VERSION() AS version');
    this.version = String(rows?.[0]?.version ?? 'unknown');
  }

  private async registry(id: string): Promise<KnowledgeBase> {
    const rows = await this.client.execute('SELECT * FROM edge_kb_registry WHERE id = ?', [id]);
    const row = rows?.[0];
    if (!row) throw new HttpError(404, '知识库不存在，请重新选择');
    const model = row.model as ModelId;
    if (!MODELS[model] || row.model_key !== modelKey(model)) throw new HttpError(409, '模型版本已变化，请新建知识库并重新向量化');
    return { id: row.id, name: row.name, model, modelKey: row.model_key, revision: Number(row.revision), indexedRevision: Number(row.indexed_revision), count: 0, createdAt: String(row.created_at) };
  }

  // Explicit null also on reload: the SDK otherwise attempts to restore a default server embedder.
  private collection(id: string) { return this.client.getCollection({ name: id, embeddingFunction: null }); }

  async list() {
    await this.ready();
    return this.queue.run(async () => {
      const rows = await this.client.execute('SELECT id FROM edge_kb_registry ORDER BY created_at, id');
      const result: KnowledgeBase[] = [];
      for (const row of rows ?? []) {
        const kb = await this.registry(row.id);
        kb.count = Number(await (await this.collection(kb.id)).count());
        result.push(kb);
      }
      return result;
    });
  }

  async create(name: string, model: ModelId) {
    await this.ready();
    return this.queue.run(async () => {
      const id = `kb_${randomUUID().replaceAll('-', '')}`;
      await this.client.createCollection({ name: id, schema: new Schema({
        fulltextIndex: new FulltextIndexConfig('ik', { ik_mode: 'smart' }),
        vectorIndex: new VectorIndexConfig({ hnsw: { dimension: MODELS[model].dimensions, distance: 'cosine' }, embeddingFunction: null }),
      }) });
      try {
        await this.client.execute('INSERT INTO edge_kb_registry (id, name, model, model_key) VALUES (?, ?, ?, ?)', [id, name, model, modelKey(model)]);
      } catch (error) { await this.client.deleteCollection(id); throw error; }
      return this.registry(id);
    });
  }

  async remove(id: string) {
    await this.ready();
    return this.queue.run(async () => {
      await this.registry(id);
      await this.client.deleteCollection(id);
      await this.client.execute('DELETE FROM edge_kb_registry WHERE id = ?', [id]);
    });
  }

  async write(id: string, key: string, chunks: Chunk[], refresh: boolean) {
    await this.ready();
    const started = performance.now();
    return this.queue.run(async () => {
      const kb = await this.registry(id);
      if (key !== kb.modelKey) throw new HttpError(409, '向量模型与知识库不一致');
      const dims = MODELS[kb.model].dimensions;
      if (chunks.some(c => c.embedding.length !== dims || !c.embedding.every(Number.isFinite) || Math.abs(Math.hypot(...c.embedding) - 1) > 0.03)) {
        throw new HttpError(400, `需要 ${dims} 维、有限且 L2 归一化的向量`);
      }
      const collection = await this.collection(id);
      const writeStart = performance.now();
      await this.client.execute('START TRANSACTION');
      try {
        await collection.upsert({ ids: chunks.map(c => c.id), documents: chunks.map(c => c.text),
          embeddings: chunks.map(c => c.embedding), metadatas: chunks.map(c => ({ source: c.source, title: c.title, part: c.part })) });
        await this.client.execute('UPDATE edge_kb_registry SET revision = revision + 1 WHERE id = ?', [id]);
        await this.client.execute('COMMIT');
      } catch (error) { await this.client.execute('ROLLBACK'); throw error; }
      const writeMs = performance.now() - writeStart;
      const refreshStart = performance.now();
      // Do not use refresh_index(): SDK silently skips refresh on unrecognized versions.
      if (refresh) {
        await this.client.execute('CALL dbms_index_manager.refresh()');
        await this.client.execute('UPDATE edge_kb_registry SET indexed_revision = revision WHERE id = ?', [id]);
      }
      return { written: chunks.length, revision: kb.revision + 1, indexed: refresh, writeMs,
        refreshMs: refresh ? performance.now() - refreshStart : 0, serverMs: performance.now() - started };
    });
  }

  async refresh(id: string) {
    await this.ready();
    return this.queue.run(async () => {
      await this.registry(id);
      const started = performance.now();
      await this.client.execute('CALL dbms_index_manager.refresh()');
      await this.client.execute('UPDATE edge_kb_registry SET indexed_revision = revision WHERE id = ?', [id]);
      return { refreshMs: performance.now() - started, indexed: true };
    });
  }

  async search(id: string, key: string, query: string, embedding: number[], k: number, mode: 'hybrid' | 'vector') {
    await this.ready();
    const started = performance.now();
    return this.queue.run(async () => {
      const kb = await this.registry(id);
      if (kb.modelKey !== key) throw new HttpError(409, '查询模型与知识库不一致');
      if (embedding.length !== MODELS[kb.model].dimensions || !embedding.every(Number.isFinite) || Math.abs(Math.hypot(...embedding) - 1) > 0.03) throw new HttpError(400, '查询向量无效');
      if (kb.indexedRevision !== kb.revision) throw new HttpError(409, '索引尚未刷新，请先完成刷新');
      const collection = await this.collection(id);
      const searchStart = performance.now();
      const result = mode === 'hybrid' ? await collection.hybridSearch({
        query: { whereDocument: { $contains: query }, nResults: Math.max(20, k) },
        knn: { queryEmbeddings: [embedding], nResults: Math.max(20, k) },
        rank: { rrf: { rankConstant: 60, rankWindowSize: Math.max(20, k) } }, nResults: k,
        include: ['documents', 'metadatas', 'distances'],
      }) : await collection.query({ queryEmbeddings: [embedding], nResults: k, include: ['documents', 'metadatas', 'distances'] });
      const searchMs = performance.now() - searchStart;
      return { kbId: id, revision: kb.revision, mode, searchMs, serverMs: performance.now() - started,
        results: result.ids[0].map((docId, i) => ({ id: docId, text: result.documents?.[0]?.[i] ?? '',
          metadata: result.metadatas?.[0]?.[i] ?? {}, score: result.distances?.[0]?.[i] ?? null })) };
    });
  }

  async close() { if (this.client) await this.client.close(); }
}
