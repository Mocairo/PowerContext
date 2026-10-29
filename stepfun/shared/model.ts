export const MODELS = {
  gemma2: {
    name: 'EmbeddingGemma 2', id: 'onnx-community/embeddinggemma-2-ONNX',
    original: 'google/embeddinggemma-2', revision: 'daa72c51243991dfcaf9f9137d2c573d8f7790c0',
    dimensions: 768, pooling: 'mean', maxLength: 512,
    queryPrefix: 'task: search result | query: ', license: 'Apache-2.0',
  },
  bge: {
    name: 'BGE Small 中文', id: 'Xenova/bge-small-zh-v1.5',
    original: 'BAAI/bge-small-zh-v1.5', revision: '75c43b069aac4d136ba6bc1122f995fedcfd2781',
    dimensions: 512, pooling: 'cls', maxLength: 512,
    queryPrefix: '为这个句子生成表示以用于检索相关文章：', license: 'MIT',
  },
} as const;
export type ModelId = keyof typeof MODELS;
export const modelKey = (id: ModelId) => {
  const m = MODELS[id];
  return `${m.id}@${m.revision}:${m.pooling}:normalized:${m.dimensions}:max512`;
};
export type Precision = 'fp32' | 'fp16' | 'q8';
export type Backend = 'webgpu' | 'wasm';
export type RuntimeConfig = { model: ModelId; mode: 'auto' | Backend; precision?: Precision; batchSize: number; simulateNoGpu?: boolean };
export type RuntimeInfo = {
  model: ModelId; backend: Backend; precision: Precision; reason: string; loadMs: number;
  adapter?: string; cold: boolean;
  adapterInfo?: Record<string, string>; gpuFeatures?: string[];
  fileReadMs?: number; sessionInitMs?: number; warmupMs?: number; browserCache?: 'cold' | 'warm'; wasmThreads?: number;
};
