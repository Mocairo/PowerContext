import { AutoConfig, AutoModel, AutoTokenizer, env, type PreTrainedModel, type PreTrainedTokenizer, type Tensor } from '@huggingface/transformers';
import { MODELS, type RuntimeConfig, type RuntimeInfo, type Backend, type Precision } from '../shared/model';

env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = '/models/';
env.useBrowserCache = true;
env.cacheKey = 'edge-atlas-models-v1';
env.backends.onnx.wasm!.wasmPaths = '/ort/';
env.backends.onnx.wasm!.numThreads = typeof crossOriginIsolated !== 'undefined' && crossOriginIsolated ? Math.min(4, navigator.hardwareConcurrency || 1) : 1;

let config: RuntimeConfig;
let info: RuntimeInfo;
let gemma: PreTrainedModel | undefined;
let tokenizer: PreTrainedTokenizer | undefined;
let loadStarted = 0;
let fileReadMs = 0;
const send = (value: unknown) => self.postMessage(value);

async function create(backend: Backend, precision: Precision) {
  const m = MODELS[config.model];
  if (config.model === 'gemma2' && precision === 'fp16') throw new Error('EmbeddingGemma 2 uses INT8 or FP32; FP16 requires separate numerical validation');
  const opts = { device: backend, dtype: precision, revision: m.revision, local_files_only: true,
    progress_callback: (event: unknown) => {
      if ((event as { status?: string }).status === 'done') fileReadMs = performance.now() - loadStarted;
      send({ type: 'progress', event, elapsedMs: performance.now() - loadStarted });
    } };
  const wasmOptions = config.model === 'gemma2' && backend === 'wasm' && precision === 'q8' ? {
    model_file_name: 'model_wasm', use_external_data_format: false,
    session_options: { externalData: [{ path: 'model_quantized.onnx_data', data: 'onnx/model_quantized.onnx_data' }] },
  } : {};
  const modelConfig = await AutoConfig.from_pretrained(m.id, opts);
  if (config.model === 'gemma2') {
    // Only the text encoder is loaded, never the 470M vision/audio parameters.
    const multimodal = modelConfig as typeof modelConfig & { vision_config: unknown; audio_config: unknown };
    multimodal.vision_config = multimodal.audio_config = null;
  }
  tokenizer = await AutoTokenizer.from_pretrained(m.id, opts);
  gemma = await AutoModel.from_pretrained(m.id, { ...opts, ...wasmOptions, config: modelConfig });
}

async function dispose() {
  await gemma?.dispose(); gemma = undefined; tokenizer = undefined;
}

async function infer(texts: string[], kind: 'query' | 'document', titles?: string[]) {
  const m = MODELS[config.model];
  const inputs = texts.map((text, i) => kind === 'query' ? `${m.queryPrefix}${text}` : config.model === 'gemma2' ? `title: ${titles?.[i] || 'none'} | text: ${text}` : text);
  let vectors: number[][];
  if (gemma && tokenizer) {
    const encoded = await tokenizer(inputs, { padding: true, truncation: true, max_length: m.maxLength });
    const output = await gemma(encoded);
    if (config.model === 'gemma2') vectors = (output.sentence_embedding as Tensor).tolist() as number[][];
    else {
      const hidden = output.last_hidden_state as Tensor;
      const [batch, sequence, dims] = hidden.dims;
      vectors = Array.from({ length: batch }, (_, i) => Array.from(hidden.data.slice(i * sequence * dims, i * sequence * dims + dims), Number));
    }
  } else throw new Error('请先加载本地模型');
  return vectors.map(vector => {
    const norm = Math.hypot(...vector);
    if (vector.length !== m.dimensions || !vector.every(Number.isFinite) || !Number.isFinite(norm) || norm < 1e-8) throw new Error('模型输出了无效向量，未写入数据库');
    return vector.map(x => x / norm);
  });
}

async function fallback(reason: string) {
  await dispose();
  await create('wasm', config.precision === 'fp32' ? 'fp32' : 'q8');
  info = { ...info, backend: 'wasm', precision: config.precision === 'fp32' ? 'fp32' : 'q8', reason };
  send({ type: 'runtime', info });
}

// Queue messages so reconfiguration cannot dispose a model during inference.
let tail = Promise.resolve();
self.onmessage = event => {
  const message = event.data;
  tail = tail.then(async () => {
    try {
      if (message.type === 'load') {
        await dispose(); config = message.config; loadStarted = performance.now(); fileReadMs = 0;
        const backend = config.mode === 'wasm' ? 'wasm' : 'webgpu';
        const precision = config.precision ?? (config.model === 'gemma2' || backend === 'wasm' ? 'q8' : 'fp16');
        const cached = typeof caches !== 'undefined' ? await (await caches.open(env.cacheKey)).keys() : [];
        const suffix = precision === 'q8' ? '_quantized' : precision === 'fp16' ? '_fp16' : '';
        const base = config.model === 'gemma2' && backend === 'wasm' && precision === 'q8' ? 'model_wasm' : 'model';
        const required = ['config.json', 'tokenizer.json', `onnx/${base}${suffix}.onnx`,
          ...(config.model === 'gemma2' ? [`onnx/model${suffix}.onnx_data`] : [])];
        info = { model: config.model, backend, precision, loadMs: 0, reason: '', cold: true,
          browserCache: required.every(file => cached.some(r => r.url.includes(MODELS[config.model].id) && r.url.endsWith(file))) ? 'warm' : 'cold', wasmThreads: env.backends.onnx.wasm!.numThreads };
        let createdAt = loadStarted;
        try {
          if (backend === 'webgpu') {
            if (config.simulateNoGpu) throw new Error('验收注入：GPU adapter 不可用');
            const gpu = (navigator as Navigator & { gpu?: { requestAdapter(): Promise<{ info?: Record<string, string>; features?: Iterable<string> } | null> } }).gpu;
            const adapter = await gpu?.requestAdapter();
            if (!adapter) throw new Error('WebGPU 不可用');
            info.adapterInfo = Object.fromEntries(['vendor', 'architecture', 'device', 'description'].map(key => [key, adapter.info?.[key] ?? '']));
            info.gpuFeatures = Array.from(adapter.features ?? []);
            info.adapter = adapter.info?.description || [adapter.info?.vendor, adapter.info?.architecture].filter(Boolean).join(' ') || 'WebGPU adapter';
          }
          await create(backend, precision);
          createdAt = performance.now();
          // Warm-up also catches unsupported GPU operators and non-finite outputs.
          await infer(['本地语义检索'], 'query');
        } catch (error) {
          if (backend !== 'webgpu' || config.mode === 'webgpu') throw error;
          await fallback(`WebGPU 初始化失败：${error instanceof Error ? error.message : error}`);
          createdAt = performance.now();
          await infer(['本地语义检索'], 'query');
        }
        info.loadMs = performance.now() - loadStarted;
        info.fileReadMs = fileReadMs;
        info.sessionInitMs = Math.max(0, createdAt - loadStarted - fileReadMs);
        info.warmupMs = performance.now() - createdAt;
        send({ id: message.id, type: 'done', value: info });
      } else if (message.type === 'embed') {
        const started = performance.now();
        let vectors: number[][];
        try { vectors = await infer(message.texts, message.kind, message.titles); }
        catch (error) {
          if (info.backend !== 'webgpu' || config.mode !== 'auto') throw error;
          await fallback(`WebGPU 推理失败：${error instanceof Error ? error.message : error}`);
          vectors = await infer(message.texts, message.kind, message.titles);
        }
        info.cold = false;
        send({ id: message.id, type: 'done', value: { vectors, embedMs: performance.now() - started, info } });
      }
    } catch (error) { send({ id: message.id, type: 'error', error: error instanceof Error ? error.message : String(error) }); }
  });
};
