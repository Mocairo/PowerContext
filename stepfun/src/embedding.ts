import type { RuntimeConfig, RuntimeInfo } from '../shared/model';

export class Embedder {
  private worker = new Worker(new URL('./embedding.worker.ts', import.meta.url), { type: 'module' });
  private counter = 0;
  private pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }>();
  info?: RuntimeInfo;
  onProgress = (_event: Record<string, unknown>) => {};
  onRuntime = (_info: RuntimeInfo) => {};
  constructor() {
    this.worker.onmessage = ({ data }) => {
      if (data.type === 'progress') { this.onProgress(data); return; }
      if (data.type === 'runtime') { this.info = data.info; this.onRuntime(data.info); return; }
      const request = this.pending.get(data.id);
      if (!request) return;
      clearTimeout(request.timer); this.pending.delete(data.id);
      if (data.type === 'error') request.reject(new Error(data.error)); else request.resolve(data.value);
    };
    this.worker.onerror = event => {
      for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(new Error(event.message || '模型 Worker 异常')); }
      this.pending.clear();
    };
  }
  private rpc<T>(type: string, args: Record<string, unknown>): Promise<T> {
    const id = ++this.counter;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('模型操作超时，请重新加载页面后重试')); }, 600000);
      this.pending.set(id, { resolve: value => resolve(value as T), reject, timer });
      this.worker.postMessage({ id, type, ...args });
    });
  }
  async load(config: RuntimeConfig) { this.info = await this.rpc<RuntimeInfo>('load', { config }); this.onRuntime(this.info); return this.info; }
  embed(texts: string[], kind: 'query' | 'document', titles?: string[]) {
    return this.rpc<{ vectors: number[][]; embedMs: number; info: RuntimeInfo }>('embed', { texts, kind, titles });
  }
}
