import './style.css';
import { Embedder } from './embedding';
import { chunkText, chunkId } from '../shared/chunking';
import { MODELS, modelKey, type ModelId, type RuntimeInfo, type RuntimeConfig } from '../shared/model';
import type { KnowledgeBase, Chunk } from '../server/database';
import sample from '../data/evaluation.json';

type Source = { id?: string; title: string; text: string; source: string };
type SearchResponse = { kbId: string; revision: number; mode: string; searchMs: number; serverMs: number;
  results: { id: string; text: string; metadata: Record<string, unknown>; score: number | null }[] };
const $ = <T extends HTMLElement = HTMLElement>(selector: string) => document.querySelector<T>(selector)!;
const escape = (value: unknown) => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
const ms = (n: number) => n < 1000 ? `${n.toFixed(0)} ms` : `${(n / 1000).toFixed(2)} s`;
const icon = (name: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({
  book: '<path d="M4 4h6a3 3 0 0 1 3 3v14a5 5 0 0 0-4-2H4z"/><path d="M13 7a3 3 0 0 1 3-3h4v15h-3a5 5 0 0 0-4 2"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  upload: '<path d="M12 16V3m-4 4 4-4 4 4M4 15v5h16v-5"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 1v5m6-5v5M9 18v5m6-5v5M1 9h5m-5 6h5m12-6h5m-5 6h5"/>',
  shield: '<path d="m12 3 8 3v5c0 5-8 10-8 10S4 16 4 11V6z"/><path d="m8 12 3 3 5-6"/>',
  file: '<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8m-8 4h6"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3"/>',
  download: '<path d="M12 3v13m-4-4 4 4 4-4M4 17v4h16v-4"/>',
} as Record<string, string>)[name] ?? ''}</svg>`;

$('#app').innerHTML = `
  <aside class="sidebar">
    <a class="brand" href="/" aria-label="Edge Atlas 首页"><span class="brand-mark">${icon('book')}</span><span>edge atlas<small>LOCAL KNOWLEDGE, CONNECTED.</small></span></a>
    <div class="nav-label">WORKSPACE <span>01</span></div>
    <div class="nav-active">${icon('layers')}我的知识库<span class="tiny-tag">LOCAL</span></div>
    <div class="kb-heading"><span>知识库</span><button class="icon-button" id="new-kb" title="新建知识库" aria-label="新建知识库">${icon('plus')}</button></div>
    <div id="collections" class="collections"><p class="muted">正在连接 seekdb…</p></div>
    <div class="sidebar-bottom"><div class="privacy-icon">${icon('shield')}</div><strong>计算留在设备上</strong><p>文档与查询向量在浏览器生成，<br>由本地 seekdb 保存与检索。</p><div id="db-status" class="db-status"><i></i>连接数据库中</div></div>
  </aside>
  <main>
    <header class="topbar"><span>工作空间 <b>/</b> <span id="breadcrumb">我的知识库</span></span><div><span class="local-dot"></span> 本地运行 <span class="separator">|</span> <span class="edition">seekdb × 阶跃星辰</span></div></header>
    <div class="main-content">
      <div class="eyebrow"><span></span> YOUR SECOND BRAIN, ON YOUR DEVICE</div>
      <div class="page-heading"><div><h1>让知识，彼此连接<span>。</span></h1><p>导入你的文档。用一个问题，找到散落在字里行间的答案。</p></div><span class="outline-stamp">EDGE<br><b>ATLAS / 01</b></span></div>
      <section class="runtime-strip" aria-label="运行状态"><div class="runtime-symbol">${icon('chip')}</div><div><span class="overline">本地推理引擎</span><strong id="runtime-title">EmbeddingGemma 2 <span>尚未加载</span></strong></div><div class="runtime-divider"></div><div class="runtime-facts"><span id="precision-label">INT8 · 768 维</span><small id="runtime-note">首次使用前，请下载对应模型文件</small></div><button id="load-model" class="button button-quiet">加载模型 ${icon('arrow')}</button></section>
      <div id="notice" role="status" aria-live="polite" hidden></div>
      <div class="workspace-grid">
        <section class="search-panel"><div class="section-heading"><h2>${icon('search')}探索知识</h2><span class="subtle">SEARCH YOUR KNOWLEDGE</span></div>
          <form id="search-form"><label for="query" class="sr-only">输入检索问题</label><div class="search-box"><textarea id="query" placeholder="描述你想找的内容，不必记得原来的关键词…" rows="3" maxlength="2000"></textarea><div class="search-box-bottom"><span>${icon('shield')} 浏览器本地向量化</span><button id="search-button" class="button button-primary" type="submit">开始检索 ${icon('arrow')}</button></div></div><div class="search-options"><label><select id="search-mode" aria-label="检索模式"><option value="hybrid">混合检索 · RRF</option><option value="vector">语义检索</option></select></label><label>结果数量 <select id="top-k" aria-label="结果数量"><option>5</option><option>10</option><option>3</option></select></label><span id="query-count">0 个片段已就绪</span></div></form>
          <div id="results" aria-live="polite"><div class="empty-state"><div class="empty-art"><span>${icon('book')}</span><i></i><i></i></div><h3>每一次提问，都是一次连接</h3><p>先导入一些文档，或载入示例知识库，<br>开始探索你的第一条知识线索。</p><div class="example-queries"><button data-query="浏览器不支持 WebGPU 时怎么办？">WebGPU 不可用怎么办？ ↗</button><button data-query="如何保证新导入的文档能被检索到？">新增文档何时可检索？ ↗</button></div></div></div>
        </section>
        <aside class="right-rail"><section class="import-panel"><div class="section-heading"><h2>${icon('upload')}添加知识</h2><span class="step-number">01</span></div><label class="dropzone" id="dropzone" for="files"><div>${icon('plus')}</div><strong>把文档放到这里</strong><p>拖拽文件，或<span>点击选择</span></p><small>TXT / MD / JSONL · 单批 ≤ 20 MB</small></label><input id="files" type="file" accept=".txt,.md,.jsonl" multiple hidden /><div class="or-line"><span>或者，从这里开始</span></div><button id="sample-import" class="button button-sample">${icon('book')}载入示例语料<span>24 篇</span></button><details class="paste-input"><summary>粘贴一段文字</summary><label for="paste-title">文档标题</label><input id="paste-title" placeholder="未命名文档" maxlength="150" /><label for="paste-text">文档内容</label><textarea id="paste-text" rows="4" placeholder="在这里粘贴文本…"></textarea><button id="paste-import" class="button button-quiet">导入文本 ${icon('arrow')}</button></details><div id="import-progress" hidden><div><span id="progress-label">正在准备</span><b id="progress-count"></b></div><progress id="progress-bar" max="100" value="0"></progress></div></section>
          <section class="settings-panel"><div class="section-heading"><h2>${icon('chip')}推理设置</h2><span class="subtle">ON DEVICE</span></div><label for="model-select">模型 <small>新建知识库时生效</small></label><select id="model-select"><option value="gemma2">EmbeddingGemma 2 · 多语言</option><option value="bge">BGE Small · 轻量中文</option></select><div class="setting-row"><label for="backend">推理后端</label><select id="backend"><option value="auto">自动 · WebGPU 优先</option><option value="wasm">WASM · 强制 CPU</option><option value="webgpu">WebGPU · 严格验证</option></select></div><div class="setting-row"><label for="batch-size">批处理大小</label><select id="batch-size"><option>4</option><option>1</option><option>8</option><option>16</option></select></div><p class="setting-note">自动模式下，GPU 初始化或推理失败会降级到 WASM。当前知识库固定使用同一模型。</p><button id="refresh-index" class="text-button">${icon('refresh')}刷新当前知识库索引</button></section>
        </aside>
      </div>
      <section class="metrics-panel"><div class="metrics-title"><div><span class="overline">PERFORMANCE, MEASURED</span><h2>每一步，都有迹可循</h2></div><button id="export-metrics" class="text-button">${icon('download')}导出运行记录</button></div><div class="metrics-grid"><div><span>模型加载与预热</span><strong id="metric-load">—</strong><small>文件读取 + 会话创建 + 预热</small></div><div><span>文档向量化</span><strong id="metric-embed">—</strong><small id="metric-throughput">浏览器 Worker 内计算</small></div><div><span>写入 / 索引刷新</span><strong id="metric-write">—</strong><small>seekdb 内完成</small></div><div><span>查询端到端</span><strong id="metric-query">—</strong><small id="metric-search">向量化 → 数据库 → 结果绘制</small></div></div></section>
      <footer><span>EDGE ATLAS <b>·</b> 你的知识，留在你的设备。</span><span>POWERED BY SEEKDB & TRANSFORMERS.JS</span></footer>
    </div>
  </main>
  <dialog id="create-dialog"><form id="create-form"><div class="eyebrow">A NEW COLLECTION</div><h2>给你的知识，一个空间。</h2><label for="kb-name">知识库名称</label><input id="kb-name" placeholder="例如：我的研究笔记" required maxlength="100" /><p>使用 <b id="dialog-model">EmbeddingGemma 2</b>，模型在创建后固定。</p><div class="dialog-actions"><button type="button" id="cancel-create" class="button button-quiet">取消</button><button class="button button-primary" type="submit">创建知识库 ${icon('arrow')}</button></div></form></dialog>`;

async function api<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(`/api${path}`, body === undefined ? { method: 'GET' } : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data as T;
}

export class EdgeAtlas {
  embedder = new Embedder();
  collections: KnowledgeBase[] = [];
  active?: KnowledgeBase;
  config: RuntimeConfig = { model: 'gemma2', mode: 'auto', batchSize: 4 };
  busy = false;
  generation = 0;
  records: Record<string, unknown>[] = [];
  private loadedKey = '';
  constructor() {
    this.embedder.onRuntime = info => this.renderRuntime(info);
    this.embedder.onProgress = data => {
      const event = data.event as Record<string, unknown>;
      if (event.status === 'progress') this.notice(`加载模型文件 ${event.file ?? ''} · ${Number(event.progress ?? 0).toFixed(0)}%`, 'info');
    };
  }
  notice(message: string, type = 'info') { const el = $('#notice'); el.hidden = false; el.className = `notice ${type}`; el.textContent = message; }
  setBusy(value: boolean) {
    this.busy = value;
    document.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLSelectElement>('button, select, input, textarea').forEach(el => { el.disabled = value; });
    $('.dropzone').classList.toggle('disabled', value);
  }
  async perform(fn: () => Promise<unknown>) {
    if (this.busy) return;
    this.setBusy(true);
    try { await fn(); } catch (error) { this.notice(error instanceof Error ? error.message : String(error), 'error'); }
    finally { this.setBusy(false); }
  }
  async init() {
    try {
      const health = await api<{ version: string }>('/health');
      $('#db-status').innerHTML = '<i></i>seekdb 已连接';
      $('#db-status').title = health.version;
      await this.reload();
      const remembered = localStorage.getItem('edge-atlas-active');
      this.select(this.collections.find(c => c.id === remembered)?.id ?? this.collections[0]?.id);
      if (!this.active) this.notice('连接已就绪。先创建知识库，或点击「载入示例语料」自动创建。');
    } catch (error) { $('#db-status').textContent = 'seekdb 未连接'; this.notice(`数据库连接失败：${error instanceof Error ? error.message : error}。请先运行数据库启动命令。`, 'error'); }
  }
  async reload() {
    this.collections = await api<KnowledgeBase[]>('/collections');
    if (this.active) this.active = this.collections.find(c => c.id === this.active!.id);
    this.renderCollections();
  }
  renderCollections() {
    $('#collections').innerHTML = this.collections.length ? this.collections.map(kb => `<button class="collection ${this.active?.id === kb.id ? 'selected' : ''}" data-kb="${kb.id}" aria-label="切换到知识库 ${escape(kb.name)}" title="${escape(kb.name)}"><span>${icon('book')}</span><span>${escape(kb.name)}<small>${MODELS[kb.model].name}</small></span><b>${kb.count}</b></button>`).join('') : '<p class="no-collections">还没有知识库<br><small>点击 +，收好你的第一份知识。</small></p>';
    document.querySelectorAll<HTMLButtonElement>('[data-kb]').forEach(btn => { btn.onclick = () => { if (!this.busy) this.select(btn.dataset.kb); }; });
    $('#query-count').textContent = `${this.active?.count ?? 0} 个片段${this.active && this.active.revision !== this.active.indexedRevision ? '待刷新' : '已就绪'}`;
  }
  select(id?: string) {
    this.active = this.collections.find(c => c.id === id); this.generation++;
    if (this.active) {
      localStorage.setItem('edge-atlas-active', this.active.id);
      this.config.model = this.active.model;
      $('#breadcrumb').textContent = this.active.name;
      $<HTMLSelectElement>('#model-select').value = this.active.model;
      if (this.active.revision !== this.active.indexedRevision) this.notice('此知识库有未刷新的写入，请点击「刷新当前知识库索引」。');
    } else {
      $('#breadcrumb').textContent = '我的知识库';
      $('#results').innerHTML = '<div class="empty-state compact"><div class="empty-art"><span>' + icon('book') + '</span></div><h3>从一份文档开始</h3><p>创建或选择知识库，连接你的知识。</p></div>';
    }
    this.renderCollections();
    if (!this.embedder.info || this.embedder.info.model !== this.config.model) this.renderUnloaded();
    if (this.active) $('#results').innerHTML = `<div class="empty-state compact"><div class="empty-art"><span>${icon('book')}</span></div><h3>${escape(this.active.name)}</h3><p>${this.active.count ? '知识已就绪，试着提出你的第一个问题。' : '这里还是一张白纸。导入文档，连接你的知识。'}</p></div>`;
  }
  renderUnloaded() {
    const m = MODELS[this.config.model];
    $('#runtime-title').innerHTML = `${m.name} <span>尚未加载</span>`;
    $('#precision-label').textContent = `INT8 · ${m.dimensions} 维`;
    $('#runtime-note').textContent = '模型按需加载 · 本地文件 · 无推理 API';
  }
  renderRuntime(info: RuntimeInfo) {
    $('#runtime-title').innerHTML = `${MODELS[info.model].name} <span class="backend-badge">${info.backend.toUpperCase()}</span>`;
    $('#precision-label').textContent = `${info.precision === 'q8' ? 'INT8' : info.precision.toUpperCase()} · ${MODELS[info.model].dimensions} 维`;
    $('#runtime-note').textContent = info.reason || (info.backend === 'webgpu' ? `${info.adapter ?? 'GPU 加速'} · 计算在当前设备完成` : 'CPU 本地计算 · WASM SIMD');
    $('#runtime-note').title = info.reason;
    $('#metric-load').textContent = ms(info.loadMs);
    $('#load-model').innerHTML = `重新加载 ${icon('refresh')}`;
  }
  async load(force = false) {
    const key = JSON.stringify([this.config.model, this.config.mode, this.config.precision, this.config.simulateNoGpu ?? false]);
    if (!force && this.loadedKey === key && this.embedder.info) return this.embedder.info;
    this.loadedKey = '';
    const model = MODELS[this.config.model];
    const manifestResponse = await fetch(`/models/${model.id}/manifest.json`);
    if (!manifestResponse.ok || !manifestResponse.headers.get('content-type')?.includes('json')) throw new Error(`模型尚未准备好，请运行 npm run models:download -- --model=${this.config.model}，等待下载完成后重试`);
    const manifest = await manifestResponse.json();
    const requiredPrecision = this.config.precision ?? (this.config.model === 'gemma2' || this.config.mode === 'wasm' ? 'q8' : 'fp16');
    if (manifest.revision !== model.revision || !manifest.sizes?.[requiredPrecision]) throw new Error(`缺少固定版本的 ${requiredPrecision} 模型，请运行 npm run models:download -- --model=${this.config.model} --precisions=${requiredPrecision},q8`);
    this.notice('正在加载与预热模型，首次加载需要一些时间…');
    const info = await this.embedder.load(this.config);
    this.loadedKey = key;
    this.records.push({ type: 'load', ...info, timestamp: new Date().toISOString() });
    this.notice(`${MODELS[info.model].name} 已就绪 · ${info.backend.toUpperCase()} / ${info.precision === 'q8' ? 'INT8' : info.precision.toUpperCase()}${info.reason ? ` · ${info.reason}` : ''}`, 'success');
    return info;
  }
  async create(name: string, model: ModelId = this.config.model) {
    const kb = await api<KnowledgeBase>('/collections', { name, model });
    await this.reload(); this.select(kb.id); return kb;
  }
  async importSources(sources: Source[], refreshEveryBatch = false) {
    if (!sources.length) throw new Error('没有可导入的非空文本');
    if (!this.active) await this.create('我的第一座知识库');
    const kb = this.active!;
    const started = performance.now();
    await this.load();
    const chunks: Omit<Chunk, 'embedding'>[] = [];
    for (const source of sources) {
      const parts = chunkText(source.text);
      for (const [part, text] of parts.entries()) chunks.push({ id: source.id && parts.length === 1 ? source.id : await chunkId(source.source, part, text), text, source: source.source, title: source.title, part });
    }
    if (!chunks.length) throw new Error('文件中没有可导入的文本');
    $('#import-progress').hidden = false;
    let embedMs = 0, writeMs = 0, refreshMs = 0, networkMs = 0;
    for (let offset = 0; offset < chunks.length; offset += this.config.batchSize) {
      const batch = chunks.slice(offset, offset + this.config.batchSize);
      $('#progress-label').textContent = '本地向量化 → 写入 seekdb';
      $('#progress-count').textContent = `${offset} / ${chunks.length}`;
      const encoded = await this.embedder.embed(batch.map(c => c.text), 'document', batch.map(c => c.title));
      embedMs += encoded.embedMs;
      const writeStart = performance.now();
      const written = await api<{ writeMs: number; refreshMs: number }>(`/collections/${kb.id}/chunks`, { modelKey: kb.modelKey,
        chunks: batch.map((c, i) => ({ ...c, embedding: encoded.vectors[i] })), refresh: refreshEveryBatch });
      networkMs += performance.now() - writeStart; writeMs += written.writeMs; refreshMs += written.refreshMs;
      $<HTMLProgressElement>('#progress-bar').value = (offset + batch.length) / chunks.length * 100;
      $('#progress-count').textContent = `${offset + batch.length} / ${chunks.length}`;
    }
    if (!refreshEveryBatch) {
      $('#progress-label').textContent = '刷新索引，等待新增内容可见';
      const refreshed = await api<{ refreshMs: number }>(`/collections/${kb.id}/refresh`, {});
      refreshMs += refreshed.refreshMs;
    }
    await this.reload();
    const metrics = { type: 'import', kbId: kb.id, sources: sources.length, chunks: chunks.length, embedMs, writeMs, refreshMs, networkMs,
      totalMs: performance.now() - started, throughput: chunks.length / (embedMs / 1000), batchSize: this.config.batchSize,
      refreshEveryBatch, runtime: this.embedder.info, timestamp: new Date().toISOString() };
    this.records.push(metrics);
    $('#metric-embed').textContent = ms(embedMs);
    $('#metric-throughput').textContent = `${metrics.throughput.toFixed(2)} 片段/s · batch ${this.config.batchSize}`;
    $('#metric-write').textContent = `${ms(writeMs)} / ${ms(refreshMs)}`;
    $('#progress-label').textContent = '导入完成 · 索引已刷新';
    this.notice(`${sources.length} 篇文档、${chunks.length} 个片段已写入 ${kb.name}，现在可以检索。`, 'success');
    return metrics;
  }
  async search(query: string, k = 5, mode: 'hybrid' | 'vector' = 'hybrid') {
    if (!this.active) throw new Error('请先创建知识库并导入文档');
    const kb = this.active, generation = this.generation;
    const started = performance.now();
    await this.load();
    const encoded = await this.embedder.embed([query], 'query');
    const requestStart = performance.now();
    const response = await api<SearchResponse>(`/collections/${kb.id}/search`, { modelKey: kb.modelKey, query, embedding: encoded.vectors[0], k, mode });
    const networkMs = performance.now() - requestStart;
    if (this.active?.id !== kb.id || generation !== this.generation) throw new Error('知识库已切换，此查询结果已丢弃');
    $('#results').innerHTML = `<div class="result-heading"><span>找到 ${response.results.length} 条知识线索</span><small>${mode === 'hybrid' ? 'seekdb · 全文 + 向量 · RRF' : 'seekdb · 向量相似度'}</small></div>${response.results.length ? response.results.map((result, i) => `<article class="result-card"><div class="result-number">${String(i + 1).padStart(2, '0')}</div><div class="result-content"><div class="result-meta">${icon('file')}<span>${escape(result.metadata.source || '文本文档')}</span><span>片段 ${Number(result.metadata.part ?? 0) + 1}</span></div><h3>${escape(result.metadata.title || '未命名文档')}</h3><p>${escape(result.text.length > 180 ? `${result.text.slice(0, 180)}…` : result.text)}</p><details><summary>查看原文 ↗</summary><pre>${escape(result.text)}</pre><small>文档 ID · ${escape(result.id)}</small></details></div></article>`).join('') : '<div class="empty-state compact"><h3>暂时没有找到相关内容</h3><p>换个描述，或向这个知识库添加更多文档。</p></div>'}`;
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    const metrics = { type: 'query', query, kbId: kb.id, mode, k, queryEmbedMs: encoded.embedMs, searchMs: response.searchMs,
      serverMs: response.serverMs, networkMs, totalMs: performance.now() - started, runtime: encoded.info, ids: response.results.map(r => r.id), timestamp: new Date().toISOString() };
    this.records.push(metrics);
    $('#metric-query').textContent = ms(metrics.totalMs);
    $('#metric-search').textContent = `向量化 ${ms(encoded.embedMs)} · seekdb ${ms(response.searchMs)}`;
    this.notice(`检索完成 · ${response.results.length} 条结果 · ${ms(metrics.totalMs)}`, 'success');
    return { ...response, metrics };
  }
}

const atlas = new EdgeAtlas();
$('#load-model').onclick = () => void atlas.perform(() => atlas.load(true));
$('#search-form').onsubmit = event => { event.preventDefault(); const query = $<HTMLTextAreaElement>('#query').value.trim(); if (query) void atlas.perform(() => atlas.search(query, Number($<HTMLSelectElement>('#top-k').value), $<HTMLSelectElement>('#search-mode').value as 'hybrid' | 'vector')); };
$('#new-kb').onclick = () => { $('#dialog-model').textContent = MODELS[atlas.config.model].name; $<HTMLDialogElement>('#create-dialog').showModal(); $<HTMLInputElement>('#kb-name').focus(); };
$('#cancel-create').onclick = () => $<HTMLDialogElement>('#create-dialog').close();
$('#create-form').onsubmit = event => { event.preventDefault(); const name = $<HTMLInputElement>('#kb-name').value.trim(); if (!name) return; $<HTMLDialogElement>('#create-dialog').close(); void atlas.perform(() => atlas.create(name)); };
$('#model-select').onchange = () => {
  const model = $<HTMLSelectElement>('#model-select').value as ModelId;
  atlas.config.model = model;
  if (atlas.active?.model !== model) { atlas.active = undefined; atlas.generation++; atlas.renderCollections(); $('#breadcrumb').textContent = '新模型 · 请创建或选择知识库'; $('#results').innerHTML = ''; }
  atlas.renderUnloaded();
};
$('#backend').onchange = () => { atlas.config.mode = $<HTMLSelectElement>('#backend').value as RuntimeConfig['mode']; atlas.renderUnloaded(); };
$('#batch-size').onchange = () => { atlas.config.batchSize = Number($<HTMLSelectElement>('#batch-size').value); };
$('#sample-import').onclick = () => void atlas.perform(() => atlas.importSources(sample.documents.map(d => ({ ...d, source: `示例/${d.id}.md` }))));
$('#paste-import').onclick = () => void atlas.perform(async () => {
  const text = $<HTMLTextAreaElement>('#paste-text').value.trim();
  const title = $<HTMLInputElement>('#paste-title').value.trim() || '未命名文档';
  if (!text) throw new Error('请先粘贴非空文本');
  return atlas.importSources([{ title, text, source: `${title}.txt` }]);
});
$('#refresh-index').onclick = () => void atlas.perform(async () => {
  if (!atlas.active) throw new Error('请先选择知识库');
  const result = await api<{ refreshMs: number }>(`/collections/${atlas.active.id}/refresh`, {});
  await atlas.reload(); atlas.notice(`索引已刷新 · ${ms(result.refreshMs)}`, 'success');
});
async function filesToSources(files: File[]) {
  if (files.reduce((n, f) => n + f.size, 0) > 20 * 1024 * 1024) throw new Error('单批文件超过 20 MB，请分批导入');
  const sources: Source[] = [];
  for (const file of files) {
    if (!/\.(txt|md|jsonl)$/i.test(file.name)) throw new Error(`不支持的文件：${file.name}`);
    const text = await file.text();
    if (/\.jsonl$/i.test(file.name)) {
      for (const [i, line] of text.split(/\r?\n/).entries()) {
        if (!line.trim()) continue;
        const row = JSON.parse(line) as Record<string, unknown>;
        if (typeof row.text !== 'string' || !row.text.trim()) throw new Error(`${file.name} 第 ${i + 1} 行缺少非空 text 字段`);
        sources.push({ title: String(row.title ?? `${file.name}:${i + 1}`), text: row.text, source: `${file.name}:${i + 1}` });
      }
    } else if (text.trim()) sources.push({ title: file.name.replace(/\.[^.]+$/, ''), text, source: file.name });
  }
  return sources;
}
$('#files').onchange = event => { const files = Array.from((event.target as HTMLInputElement).files ?? []); void atlas.perform(async () => atlas.importSources(await filesToSources(files))); (event.target as HTMLInputElement).value = ''; };
$('#dropzone').ondragover = event => { event.preventDefault(); if (!atlas.busy) $('#dropzone').classList.add('dragover'); };
$('#dropzone').ondragleave = () => $('#dropzone').classList.remove('dragover');
$('#dropzone').ondrop = event => { event.preventDefault(); $('#dropzone').classList.remove('dragover'); if (atlas.busy) return; const files = Array.from(event.dataTransfer?.files ?? []); void atlas.perform(async () => atlas.importSources(await filesToSources(files))); };
document.querySelectorAll<HTMLButtonElement>('[data-query]').forEach(button => { button.onclick = () => { $<HTMLTextAreaElement>('#query').value = button.dataset.query!; $<HTMLTextAreaElement>('#query').focus(); }; });
$('#export-metrics').onclick = () => {
  const blob = new Blob([JSON.stringify({ exportedAt: new Date().toISOString(), userAgent: navigator.userAgent, hardwareConcurrency: navigator.hardwareConcurrency, crossOriginIsolated, modelVersions: MODELS, records: atlas.records }, null, 2)], { type: 'application/json' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `edge-atlas-${Date.now()}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
};
// The reproducible benchmark drives exactly the application/Worker/API path.
if (new URLSearchParams(location.search).has('benchmark')) (window as unknown as { edgeAtlas: EdgeAtlas }).edgeAtlas = atlas;
void atlas.init().then(async () => {
  if (new URLSearchParams(location.search).has('benchmark')) {
    const { mountBenchmark } = await import('./benchmark');
    mountBenchmark(atlas);
  }
});
