import { mkdir, readdir, copyFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
await mkdir(resolve(root, 'public/ort'), { recursive: true });
for (const name of await readdir(resolve(root, 'node_modules/onnxruntime-web/dist'))) {
  if (/^ort-wasm.*\.(mjs|wasm)$/.test(name)) await copyFile(resolve(root, 'node_modules/onnxruntime-web/dist', name), resolve(root, 'public/ort', name));
}
console.log('ONNX Runtime Web binaries prepared locally.');
