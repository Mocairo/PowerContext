import { mkdir, stat, readFile, writeFile, rename, copyFile, unlink } from 'node:fs/promises';
import { createWriteStream, createReadStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepareWasmGraph } from './wasm-graph.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const models = {
  gemma2: { id: 'onnx-community/embeddinggemma-2-ONNX', revision: 'daa72c51243991dfcaf9f9137d2c573d8f7790c0' },
  bge: { id: 'Xenova/bge-small-zh-v1.5', revision: '75c43b069aac4d136ba6bc1122f995fedcfd2781' },
};
const modelId = process.argv.find(a => a.startsWith('--model='))?.split('=')[1] ?? 'gemma2';
const precisions = (process.argv.find(a => a.startsWith('--precisions='))?.split('=')[1] ?? 'q8').split(',');
const m = models[modelId];
if (!m || precisions.some(p => !['fp32', 'fp16', 'q8'].includes(p))) throw new Error('Use --model=gemma2|bge --precisions=fp32,fp16,q8');
const api = await fetch(`https://huggingface.co/api/models/${m.id}/revision/${m.revision}?blobs=true`);
if (!api.ok) throw new Error(`Model manifest HTTP ${api.status}`);
const metadata = await api.json();
if (metadata.sha !== m.revision) throw new Error('Revision mismatch');
const suffix = { fp32: '', fp16: '_fp16', q8: '_quantized' };
const names = ['config.json', 'tokenizer.json', 'tokenizer_config.json', ...(modelId === 'bge' ? ['special_tokens_map.json', 'vocab.txt'] : []),
  ...precisions.flatMap(p => [`onnx/model${suffix[p]}.onnx`, ...(modelId === 'gemma2' ? [`onnx/model${suffix[p]}.onnx_data`] : [])])];
const directory = resolve(root, 'public/models', m.id);
const sha256 = async path => { const hash = createHash('sha256'); for await (const chunk of createReadStream(path)) hash.update(chunk); return hash.digest('hex'); };
const records = [];
async function download(url, path, size) {
  const partial = `${path}.partial`;
  let offset = (await stat(partial).catch(() => null))?.size ?? 0;
  if (offset > size) { await unlink(partial); offset = 0; }
  if (size < 64 * 1024 * 1024) {
    const response = await fetch(url, { signal: AbortSignal.timeout(120000) });
    if (!response.ok || !response.body) throw new Error(`Download HTTP ${response.status}`);
    await pipeline(Readable.fromWeb(response.body), createWriteStream(partial));
    return;
  }
  const chunkSize = 16 * 1024 * 1024;
  const ranges = [];
  for (let start = offset; start < size; start += chunkSize) ranges.push({ start, end: Math.min(size - 1, start + chunkSize - 1) });
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(4, ranges.length) }, async () => {
    while (next < ranges.length) {
      const range = ranges[next++];
      const piece = `${partial}.${range.start}`;
      if ((await stat(piece).catch(() => null))?.size === range.end - range.start + 1) continue;
      for (let attempt = 0; ; attempt++) {
        try {
          const response = await fetch(url, { headers: { Range: `bytes=${range.start}-${range.end}` }, signal: AbortSignal.timeout(90000) });
          if (response.status !== 206 || response.headers.get('content-range') !== `bytes ${range.start}-${range.end}/${size}`) { await response.body?.cancel(); throw new Error('Server did not honor byte range'); }
          await pipeline(Readable.fromWeb(response.body), createWriteStream(piece));
          if ((await stat(piece)).size !== range.end - range.start + 1) throw new Error('Incomplete range');
          break;
        } catch (error) { if (attempt >= 3) throw error; console.log(`Retrying range ${range.start} (${attempt + 1})`); }
      }
    }
  }));
  for (const range of ranges) {
    const piece = `${partial}.${range.start}`;
    await pipeline(createReadStream(piece), createWriteStream(partial, { flags: 'a' }));
    await unlink(piece);
  }
}
for (const name of names) {
  const info = metadata.siblings.find(s => s.rfilename === name);
  if (!info) throw new Error(`Missing ${name}`);
  const path = resolve(directory, name);
  await mkdir(dirname(path), { recursive: true });
  const existing = await stat(path).catch(() => null);
  if (existing?.size !== info.size || (info.lfs?.sha256 && await sha256(path) !== info.lfs.sha256)) {
    console.log(`Downloading ${modelId}/${name} (${(info.size / 1e6).toFixed(1)} MB)`);
    await download(`https://huggingface.co/${m.id}/resolve/${m.revision}/${name}`, path, info.size);
    const downloaded = await stat(`${path}.partial`);
    if (downloaded.size !== info.size) throw new Error(`Size mismatch: ${name}`);
    if (info.lfs?.sha256 && await sha256(`${path}.partial`) !== info.lfs.sha256) throw new Error(`SHA256 mismatch: ${name}`);
    await rename(`${path}.partial`, path);
  }
  records.push({ file: name, bytes: info.size, sha256: await sha256(path) });
}
const shared = records.filter(r => !r.file.startsWith('onnx/'));
const sizes = Object.fromEntries(precisions.map(p => [p, [...shared, ...records.filter(r => r.file.startsWith(`onnx/model${suffix[p]}.onnx`))].reduce((n, r) => n + r.bytes, 0)]));
if (modelId === 'gemma2' && precisions.includes('q8')) {
  const graph = await prepareWasmGraph(directory);
  const info = await stat(graph);
  records.push({ file: 'onnx/model_wasm_quantized.onnx', bytes: info.size, sha256: await sha256(graph), derived: 'GatherBlockQuantized -> standard ONNX Gather/Cast/Sub/Mul/Reshape; identical external INT8 weights' });
  sizes.q8wasm = shared.reduce((n, r) => n + r.bytes, 0) + info.size + records.find(r => r.file === 'onnx/model_quantized.onnx_data').bytes;
}
let old = { files: [], sizes: {} };
try { old = JSON.parse(await readFile(resolve(directory, 'manifest.json'), 'utf8')); } catch {}
const manifest = { model: modelId, ...m, downloadedAt: new Date().toISOString(), files: [...old.files.filter(f => !records.some(r => r.file === f.file)), ...records], sizes: { ...old.sizes, ...sizes } };
await writeFile(resolve(directory, 'manifest.json'), JSON.stringify(manifest, null, 2));
await mkdir(resolve(root, 'public/ort'), { recursive: true });
for (const name of ['ort-wasm-simd-threaded.mjs', 'ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.jsep.mjs', 'ort-wasm-simd-threaded.jsep.wasm']) {
  await copyFile(resolve(root, 'node_modules/onnxruntime-web/dist', name), resolve(root, 'public/ort', name));
}
console.log(JSON.stringify({ ...m, sizes, ready: true }, null, 2));
