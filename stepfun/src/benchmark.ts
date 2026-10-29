import type { EdgeAtlas } from './main';
import type { RuntimeConfig, ModelId } from '../shared/model';
import { MODELS } from '../shared/model';
import { quality } from '../shared/chunking';
import dataset from '../data/evaluation.json';

const data = dataset as unknown as { version: string; documents: { id: string; title: string; text: string }[]; queries: { id: string; text: string; relevant: Record<string, number> }[] };

type Profile = { name: string; config: RuntimeConfig; refreshEveryBatch: boolean };
const percentile = (values: number[], q: number) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * q) - 1)] ?? null;
};

export function mountBenchmark(atlas: EdgeAtlas) {
  const panel = document.createElement('section');
  panel.className = 'benchmark-panel';
  panel.innerHTML = `<div class="section-heading"><h2>可复现实验台</h2><span class="subtle">真实浏览器 → 真实 seekdb</span></div><p>固定 24 篇原始语料、12 条分级标注查询。每个配置创建独立集合，预热后重复 3 轮；同时记录语义检索与数据库混合检索质量。矩阵测试可能耗时数分钟。</p><div class="benchmark-actions"><button id="benchmark-current" class="button button-primary">评测当前配置</button><button id="benchmark-matrix" class="button button-quiet">运行完整对比矩阵</button><button id="benchmark-smoke" class="button button-quiet">快速浏览器验收</button><button id="benchmark-fallback" class="button button-quiet">验证自动降级</button></div><pre id="benchmark-log" role="log">等待开始。先选定模型与后端。</pre>`;
  document.querySelector('.metrics-panel')!.before(panel);
  const log = (line: string) => { const output = document.querySelector('#benchmark-log')!; output.textContent += `${line}\n`; output.scrollTop = output.scrollHeight; };
  const run = async (profiles: Profile[], smoke = false) => {
    const originalActive = atlas.active?.id;
    const original = { ...atlas.config };
    const report: Record<string, unknown> = { schemaVersion: 1, timestamp: new Date().toISOString(),
      userAgent: navigator.userAgent, hardwareConcurrency: navigator.hardwareConcurrency, deviceMemory: (navigator as Navigator & { deviceMemory?: number }).deviceMemory,
      crossOriginIsolated, datasetVersion: data.version, corpusSize: data.documents.length, queryCount: smoke ? 2 : data.queries.length,
      relevance: 'frozen graded judgments; gains 2^grade-1; recall denominator all relevant IDs',
      maxQualityDrop: 0.02, repeats: smoke ? 1 : 3, modelVersions: MODELS, profiles: [] };
    const reports = report.profiles as Record<string, unknown>[];
    const health = await (await fetch('/api/health')).json();
    report.database = health;
    document.querySelector('#benchmark-log')!.textContent = '';
    for (const profile of profiles) {
      let kbId: string | undefined;
      const result: Record<string, unknown> = { profile, status: 'running' };
      reports.push(result);
      try {
        atlas.config = { ...profile.config };
        log(`▶ ${profile.name}：加载模型…`);
        const kb = await atlas.create(`实验 ${profile.name} ${Date.now()}`, profile.config.model);
        kbId = kb.id;
        const runtime = await atlas.load(true);
        if (profile.config.mode !== 'auto' && runtime.backend !== profile.config.mode) throw new Error(`后端不匹配：实际 ${runtime.backend}`);
        result.runtime = { ...runtime };
        const manifest = await (await fetch(`/models/${MODELS[profile.config.model].id}/manifest.json`)).json();
        result.modelFiles = manifest;
        log(`  ${runtime.backend}/${runtime.precision} · 加载与预热 ${runtime.loadMs.toFixed(0)} ms；开始导入`);
        const imported = await atlas.importSources(data.documents.map(d => ({ ...d, source: `evaluation/${d.id}.md` })), profile.refreshEveryBatch);
        result.import = imported;
        log(`  ${imported.chunks} 个片段 · ${imported.throughput.toFixed(2)} 片段/s · 刷新 ${imported.refreshMs.toFixed(0)} ms`);
        const queries = smoke ? data.queries.slice(0, 2) : data.queries;
        const vectorResults: unknown[] = [], hybridResults: unknown[] = [];
        for (let round = 0; round < (smoke ? 1 : 3); round++) {
          for (const query of queries) {
            if (round === 0) {
              const response = await atlas.search(query.text, 5, 'vector');
              vectorResults.push({ queryId: query.id, round, ...response.metrics, quality: quality(response.results.map(r => r.id), query.relevant as Record<string, number>, 5) });
            }
            const response = await atlas.search(query.text, 5, 'hybrid');
            hybridResults.push({ queryId: query.id, round, ...response.metrics, quality: quality(response.results.map(r => r.id), query.relevant as Record<string, number>, 5) });
          }
          log(`  查询第 ${round + 1} 轮完成`);
        }
        result.vector = vectorResults; result.hybrid = hybridResults;
        const summarize = (results: unknown[]) => {
          const rows = results as { totalMs: number; searchMs: number; queryEmbedMs: number; quality: { recall: number; ndcg: number } }[];
          return { recallAt5: rows.reduce((s, r) => s + r.quality.recall, 0) / rows.length,
            ndcgAt5: rows.reduce((s, r) => s + r.quality.ndcg, 0) / rows.length,
            medianQueryMs: percentile(rows.map(r => r.totalMs), 0.5), p95QueryMs: percentile(rows.map(r => r.totalMs), 0.95),
            medianSearchMs: percentile(rows.map(r => r.searchMs), 0.5), medianQueryEmbeddingMs: percentile(rows.map(r => r.queryEmbedMs), 0.5) };
        };
        result.summary = { vector: summarize(vectorResults), hybrid: summarize(hybridResults) };
        result.firstWarmQuery = hybridResults[0];
        result.status = 'passed';
        log(`✓ ${profile.name}：${JSON.stringify(result.summary)}`);
      } catch (error) {
        result.status = 'failed'; result.error = error instanceof Error ? error.message : String(error);
        log(`✗ ${profile.name}：${result.error}`);
      } finally {
        if (kbId) {
          const response = await fetch(`/api/collections/${kbId}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          if (!response.ok) log(`清理提示：实验集合 ${kbId} 保留，请手动检查`);
        }
      }
    }
    atlas.config = original;
    await atlas.reload(); atlas.select(originalActive ?? atlas.collections[0]?.id);
    const resourceEntries = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    report.resourceAudit = resourceEntries.map(r => ({ name: r.name, initiatorType: r.initiatorType, duration: r.duration, transferSize: r.transferSize }));
    report.completedAt = new Date().toISOString();
    report.allPassed = reports.every(p => p.status === 'passed');
    const baseline = reports.find(p => (p.profile as Profile).config.mode === 'wasm' && (p.profile as Profile).config.precision === 'fp32' && p.status === 'passed');
    if (baseline) {
      const reference = baseline.summary as Record<string, { recallAt5: number; ndcgAt5: number }>;
      report.qualityComparison = reports.filter(p => p.status === 'passed' && p !== baseline).map(p => {
        const summary = p.summary as typeof reference;
        const differences = Object.fromEntries(['vector', 'hybrid'].map(mode => [mode, {
          recallDrop: reference[mode].recallAt5 - summary[mode].recallAt5,
          ndcgDrop: reference[mode].ndcgAt5 - summary[mode].ndcgAt5,
        }]));
        return { profile: (p.profile as Profile).name, differences, passed: Object.values(differences).every(d => d.recallDrop <= 0.02 && d.ndcgDrop <= 0.02) };
      });
    }
    const save = await fetch('/api/benchmarks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(report) });
    const saved = await save.json();
    log(`原始记录：${saved.path ?? saved.error}`);
    atlas.notice(`评测结束 · ${reports.filter(p => p.status === 'passed').length}/${reports.length} 个配置通过 · ${saved.path ?? '保存失败'}`, report.allPassed ? 'success' : 'error');
  };
  const current = () => ({ name: `${atlas.config.model}-${atlas.config.mode}-${atlas.config.precision ?? 'quantized'}-b${atlas.config.batchSize}`, config: { ...atlas.config }, refreshEveryBatch: false });
  document.querySelector<HTMLButtonElement>('#benchmark-current')!.onclick = () => void atlas.perform(() => run([current()]));
  document.querySelector<HTMLButtonElement>('#benchmark-smoke')!.onclick = () => void atlas.perform(() => run([current()], true));
  document.querySelector<HTMLButtonElement>('#benchmark-fallback')!.onclick = () => void atlas.perform(() => run([{ name: `${atlas.config.model}-auto-adapter-unavailable-injection`, config: { ...atlas.config, mode: 'auto', simulateNoGpu: true }, refreshEveryBatch: false }], true));
  document.querySelector<HTMLButtonElement>('#benchmark-matrix')!.onclick = () => void atlas.perform(() => {
    const model: ModelId = atlas.config.model;
    const profiles: Profile[] = [
      { name: `${model}-wasm-fp32-b1-refresh-each`, config: { model, mode: 'wasm', precision: 'fp32', batchSize: 1 }, refreshEveryBatch: true },
      { name: `${model}-wasm-q8-b1-refresh-each`, config: { model, mode: 'wasm', precision: 'q8', batchSize: 1 }, refreshEveryBatch: true },
      { name: `${model}-wasm-q8-b4-refresh-once`, config: { model, mode: 'wasm', precision: 'q8', batchSize: 4 }, refreshEveryBatch: false },
      ...(model === 'bge' ? [{ name: `${model}-webgpu-fp32-b4-refresh-once`, config: { model, mode: 'webgpu', precision: 'fp32', batchSize: 4 } as RuntimeConfig, refreshEveryBatch: false }] : []),
      { name: `${model}-webgpu-${model === 'gemma2' ? 'q8' : 'fp16'}-b4-refresh-once`, config: { model, mode: 'webgpu', precision: model === 'gemma2' ? 'q8' : 'fp16', batchSize: 4 }, refreshEveryBatch: false },
    ];
    return run(profiles);
  });
}
