import { readFile, readdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { quality } from '../shared/chunking';
import dataset from '../data/evaluation.json';
import assert from 'node:assert/strict';

const percentile = (values: number[], q: number) => [...values].sort((a, b) => a - b)[Math.max(0, Math.ceil(values.length * q) - 1)];

const filename = process.argv[2] ?? (await readdir('artifacts').catch(() => [])).filter(f => /^benchmark-.*\.json$/.test(f)).sort().at(-1);
if (!filename) {
  console.log('Open http://127.0.0.1:4177/?benchmark=1 and click 运行完整对比矩阵. Then run npm run benchmark -- artifacts/<raw-log>.json to verify and summarize the raw browser results.');
  process.exit(0);
}
const path = filename.includes('/') || filename.includes('\\') ? filename : `artifacts/${filename}`;
const raw = await readFile(path, 'utf8');
const report = JSON.parse(raw);
const digest = createHash('sha256').update(raw).digest('hex');
const queries = new Map(dataset.queries.map(q => [q.id, q]));
const lines = ['# 浏览器实测结果', '', `原始数据：\`${path}\``, `SHA-256：\`${digest}\``, '', `环境：${report.userAgent}; ${report.hardwareConcurrency} logical CPUs; crossOriginIsolated=${report.crossOriginIsolated}`, '', `seekdb：${report.database?.version ?? 'unknown'}。语料 ${report.corpusSize} 篇，查询 ${report.queryCount} 条，每档 ${report.repeats} 轮。`, '', '所有时间来自提交应用的 Worker/API/结果绘制路径。语义检索与混合检索分别评分，避免全文检索掩盖 embedding 变化。此数据集是小型合成验收集，不能代表公开标准榜单。', ''];
for (const profile of report.profiles) {
  lines.push(`## ${profile.profile.name}`, '');
  if (profile.status !== 'passed') { lines.push(`未通过：${profile.error}`, ''); continue; }
  for (const mode of ['vector', 'hybrid']) {
    const rows = profile[mode];
    assert.equal(rows.length, report.queryCount * (mode === 'hybrid' ? report.repeats : 1), 'Missing query samples');
    const queryIds = new Set<string>();
    for (const row of rows) {
      const query = queries.get(row.queryId);
      if (!query) throw new Error(`Unknown query ${row.queryId}`);
      const result = quality(row.ids, query.relevant as unknown as Record<string, number>, 5);
      if (Math.abs(result.recall - row.quality.recall) > 1e-10 || Math.abs(result.ndcg - row.quality.ndcg) > 1e-10) throw new Error(`Metric verification failed: ${profile.profile.name} ${row.queryId}`);
      assert(!queryIds.has(`${row.round}:${row.queryId}`), 'Duplicate query sample');
      queryIds.add(`${row.round}:${row.queryId}`);
    }
    const mean = (fn: (r: any) => number) => rows.reduce((n: number, r: any) => n + fn(r), 0) / rows.length;
    const expected = { recallAt5: mean(r => r.quality.recall), ndcgAt5: mean(r => r.quality.ndcg),
      medianQueryMs: percentile(rows.map((r: any) => r.totalMs), 0.5), p95QueryMs: percentile(rows.map((r: any) => r.totalMs), 0.95),
      medianSearchMs: percentile(rows.map((r: any) => r.searchMs), 0.5), medianQueryEmbeddingMs: percentile(rows.map((r: any) => r.queryEmbedMs), 0.5) };
    for (const [key, value] of Object.entries(expected)) assert(Math.abs(profile.summary[mode][key] - value) < 1e-8, `Summary mismatch: ${key}`);
  }
  assert(Math.abs(profile.import.throughput - profile.import.chunks / (profile.import.embedMs / 1000)) < 1e-8);
  const summary = profile.summary;
  const sizeKey = profile.runtime.model === 'gemma2' && profile.runtime.backend === 'wasm' && profile.runtime.precision === 'q8' ? 'q8wasm' : profile.runtime.precision;
  const bytes = profile.modelFiles.sizes[sizeKey];
  lines.push(`实际后端 ${profile.runtime.backend}，精度 ${profile.runtime.precision}，批次 ${profile.profile.config.batchSize}，模型与分词器实际总文件 ${bytes.toLocaleString('en-US')} 字节。`, '',
    `- 模型加载与预热：${profile.runtime.loadMs.toFixed(2)} ms；文档向量化吞吐：${profile.import.throughput.toFixed(2)} 片段/s。`,
    `- 写入：${profile.import.writeMs.toFixed(2)} ms；索引刷新：${profile.import.refreshMs.toFixed(2)} ms。`,
    `- 混合检索：数据库中位数 ${summary.hybrid.medianSearchMs.toFixed(2)} ms；端到端中位数 ${summary.hybrid.medianQueryMs.toFixed(2)} ms，p95 ${summary.hybrid.p95QueryMs.toFixed(2)} ms。`,
    `- 语义检索 Recall@5=${summary.vector.recallAt5.toFixed(6)}，nDCG@5=${summary.vector.ndcgAt5.toFixed(6)}。`,
    `- 混合检索 Recall@5=${summary.hybrid.recallAt5.toFixed(6)}，nDCG@5=${summary.hybrid.ndcgAt5.toFixed(6)}。`, '');
}
if (report.qualityComparison) lines.push('## 对同模型 FP32 基线的质量差异', '', '预先冻结门槛：Recall@5 与 nDCG@5 的绝对下降各不超过 0.02。', '', ...report.qualityComparison.map((c: { profile: string; differences: unknown; passed: boolean }) => `- ${c.profile}：${JSON.stringify(c.differences)}；${c.passed ? '通过' : '超过门槛'}。`), '');
await writeFile('artifacts/results.md', lines.join('\n'));
console.log(`Raw metric recomputation passed. Saved artifacts/results.md. Source SHA256 ${digest}`);
