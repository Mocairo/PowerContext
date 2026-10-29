import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { Database } from '../server/database';
import { modelKey } from '../shared/model';

// Synthetic unit vectors test ONLY the database contract, never inference/quality/performance.
const db = new Database();
const created: string[] = [];
const checks: { check: string; passed: boolean }[] = [];
const unit = (i: number) => Array.from({ length: 512 }, (_, n) => n === i ? 1 : 0);
try {
  const a = await db.create('integration A', 'bge'), b = await db.create('integration B', 'bge');
  created.push(a.id, b.id);
  const row = { id: 'first', text: '索引刷新确保新增文档可检索。事务提交后进行刷新。', source: 'first.txt', title: '索引刷新', part: 0, embedding: unit(0) };
  await db.write(a.id, modelKey('bge'), [row], false);
  await assert.rejects(() => db.search(a.id, modelKey('bge'), '索引刷新', unit(0), 5, 'hybrid'), /索引尚未刷新/);
  checks.push({ check: 'pending writes block search until explicit refresh', passed: true });
  await db.refresh(a.id);
  const first = await db.search(a.id, modelKey('bge'), '索引刷新', unit(0), 5, 'hybrid');
  assert.equal(first.results[0]?.id, 'first');
  checks.push({ check: 'real seekdb hybrid search returns newly refreshed text', passed: true });
  await db.write(a.id, modelKey('bge'), [row], true);
  assert.equal((await db.list()).find(k => k.id === a.id)?.count, 1);
  checks.push({ check: 'idempotent upsert does not duplicate chunks', passed: true });
  await db.write(a.id, modelKey('bge'), [{ ...row, id: 'second', text: '阳台番茄喜欢阳光和排水良好的土壤。', embedding: unit(1) }], true);
  assert.equal((await db.list()).find(k => k.id === a.id)?.count, 2);
  assert.equal((await db.search(a.id, modelKey('bge'), '番茄', unit(1), 1, 'hybrid')).results[0]?.id, 'second');
  checks.push({ check: 'second incremental import is immediately searchable', passed: true });
  await db.write(b.id, modelKey('bge'), [{ ...row, id: 'private-b', text: '咖啡萃取需要合适的温度。', embedding: unit(2) }], true);
  const concurrent = await Promise.all(Array.from({ length: 12 }, (_, i) => db.search(i % 2 ? a.id : b.id, modelKey('bge'), i % 2 ? '索引刷新' : '咖啡', unit(i % 2 ? 0 : 2), 1, 'hybrid')));
  concurrent.forEach((result, i) => assert.equal(result.results[0]?.id, i % 2 ? 'first' : 'private-b'));
  checks.push({ check: '12 concurrent hybrid queries preserve collection/session isolation', passed: true });
  await assert.rejects(() => db.write(a.id, modelKey('gemma2'), [row], true), /模型/);
  await assert.rejects(() => db.write(a.id, modelKey('bge'), [{ ...row, embedding: [1] }], true), /512/);
  await assert.rejects(() => db.write(a.id, modelKey('bge'), [{ ...row, embedding: Array(512).fill(0) }], true), /归一化/);
  checks.push({ check: 'wrong model, dimensions and zero vectors rejected', passed: true });
  const reopened = new Database();
  try {
    const persisted = await reopened.list();
    assert.equal(persisted.find(k => k.id === a.id)?.count, 2);
    assert.equal((await reopened.search(a.id, modelKey('bge'), '番茄', unit(1), 1, 'hybrid')).results[0]?.id, 'second');
  } finally { await reopened.close(); }
  checks.push({ check: 'new SDK connection restores registry and vectors without server embedder', passed: true });
  console.log(JSON.stringify(checks, null, 2));
  await mkdir('artifacts', { recursive: true });
  await writeFile('artifacts/database-validation.json', JSON.stringify({ timestamp: new Date().toISOString(), databaseVersion: db.version, syntheticVectors: true, note: 'Database contract test only; not embedding benchmark evidence', checks }, null, 2));
} finally {
  for (const id of created) await db.remove(id);
  await db.close();
}
