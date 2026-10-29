import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
// Pinned ONNX Runtime Web ships this protobuf schema; no Python or inference is needed.
const { onnx } = require(resolve(root, 'node_modules/onnxruntime-web/lib/onnxjs/ort-schema/protobuf/onnx.js'));

export function lowerGather(model) {
  const replacements = [];
  const tensors = new Map(model.graph.initializer.map(t => [t.name, t]));
  let lowered = 0;
  for (const node of model.graph.node) {
    if (node.opType !== 'GatherBlockQuantized') { replacements.push(node); continue; }
    const attrs = Object.fromEntries(node.attribute.map(a => [a.name, Number(a.i)]));
    const weights = tensors.get(node.input[0]);
    const scales = tensors.get(node.input[2]);
    const zeros = tensors.get(node.input[3]);
    if (attrs.bits !== 8 || attrs.gather_axis !== 0 || attrs.quantize_axis !== 1 || weights?.dataType !== 2 || scales?.dataType !== 1 || zeros?.dataType !== 2) throw new Error('Unsupported GatherBlockQuantized contract');
    const [vocab, width] = weights.dims.map(Number);
    const block = attrs.block_size;
    const blocks = width / block;
    if (!Number.isInteger(blocks) || scales.dims.map(Number).join() !== `${vocab},${blocks}` || zeros.dims.map(Number).join() !== `${vocab},${blocks}`) throw new Error('Unexpected quantized embedding shapes');
    const prefix = `edge_wasm_gather_${lowered++}`;
    const add = (opType, inputs, output, attributes = []) => replacements.push(onnx.NodeProto.fromObject({ opType, name: `${prefix}_${output}`, input: inputs, output: [output], attribute: attributes }));
    const attr = (name, i) => ({ name, type: 2, i });
    for (const [name, values] of [['shape4', [0, 0, blocks, block]], ['shape3', [0, 0, width]], ['axes', [-1]]]) model.graph.initializer.push(onnx.TensorProto.fromObject({ name: `${prefix}_${name}`, dataType: 7, dims: [values.length], int64Data: values }));
    const n = name => `${prefix}_${name}`;
    // For every selected token: (uint8 weight - uint8 zero point) * float32 scale.
    // Only the selected rows are expanded. All original INT8 weights and bytes are retained.
    add('Gather', [node.input[0], node.input[1]], n('q'), [attr('axis', 0)]);
    add('Cast', [n('q')], n('qf'), [attr('to', 1)]);
    add('Reshape', [n('qf'), n('shape4')], n('q4'));
    add('Gather', [node.input[3], node.input[1]], n('z'), [attr('axis', 0)]);
    add('Cast', [n('z')], n('zf'), [attr('to', 1)]);
    add('Unsqueeze', [n('zf'), n('axes')], n('z4'));
    add('Gather', [node.input[2], node.input[1]], n('s'), [attr('axis', 0)]);
    add('Unsqueeze', [n('s'), n('axes')], n('s4'));
    add('Sub', [n('q4'), n('z4')], n('centered'));
    add('Mul', [n('centered'), n('s4')], n('dequantized'));
    add('Reshape', [n('dequantized'), n('shape3')], node.output[0]);
  }
  if (lowered !== 1) throw new Error(`Expected one GatherBlockQuantized, found ${lowered}`);
  model.graph.node = replacements;
  const error = onnx.ModelProto.verify(model);
  if (error) throw new Error(error);
  return model;
}

export async function prepareWasmGraph(directory) {
  const source = resolve(directory, 'onnx/model_quantized.onnx');
  const output = resolve(directory, 'onnx/model_wasm_quantized.onnx');
  const model = onnx.ModelProto.decode(await readFile(source));
  await writeFile(output, onnx.ModelProto.encode(lowerGather(model)).finish());
  return output;
}
