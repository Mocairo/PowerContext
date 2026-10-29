"""Optional development proof: pip install onnx==1.20.1 numpy. No embeddings are computed.

Executes the actual lowered standard-ONNX subgraph on synthetic quantized rows,
and compares it with the specified block-dequantization equation.
"""
import hashlib
import json
from pathlib import Path
import numpy as np
import onnx
from onnx import TensorProto, helper
from onnx.reference import ReferenceEvaluator

root = Path(__file__).resolve().parents[1]
path = root / 'public/models/onnx-community/embeddinggemma-2-ONNX/onnx/model_wasm_quantized.onnx'
model = onnx.load(path, load_external_data=False)
nodes = [n for n in model.graph.node if n.name.startswith('edge_wasm_gather_0_')]
initializers = [t for t in model.graph.initializer if t.name.startswith('edge_wasm_gather_0_')]
assert len(nodes) == 11 and len(initializers) == 3
assert not any(n.op_type == 'GatherBlockQuantized' for n in model.graph.node)
inputs = [
    helper.make_tensor_value_info('model_embed_tokens_weight_quant', TensorProto.UINT8, [9, 512]),
    helper.make_tensor_value_info('input_ids', TensorProto.INT64, [2, 4]),
    helper.make_tensor_value_info('model_embed_tokens_weight_scales', TensorProto.FLOAT, [9, 16]),
    helper.make_tensor_value_info('model_embed_tokens_weight_zp', TensorProto.UINT8, [9, 16]),
]
graph = helper.make_graph(nodes, 'actual-lowered-gather-test', inputs,
    [helper.make_tensor_value_info('/model/embed_tokens/Gather/output_0', TensorProto.FLOAT, [2, 4, 512])], initializers)
test_model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 21)])
onnx.checker.check_model(test_model)
rng = np.random.default_rng(20261029)
max_error = 0.0
for _ in range(20):
    q = rng.integers(0, 256, size=(9, 512), dtype=np.uint8)
    zp = rng.integers(0, 256, size=(9, 16), dtype=np.uint8)
    scale = rng.uniform(1e-6, 0.3, size=(9, 16)).astype(np.float32)
    ids = np.array([[0, 8, 1, -1], [4, 2, 0, 8]], dtype=np.int64)
    actual = ReferenceEvaluator(test_model).run(None, {
        inputs[0].name: q, inputs[1].name: ids, inputs[2].name: scale, inputs[3].name: zp,
    })[0]
    expected = ((q[ids].astype(np.float32).reshape(2, 4, 16, 32) - zp[ids, :, None].astype(np.float32)) * scale[ids, :, None]).reshape(2, 4, 512)
    max_error = max(max_error, float(np.max(np.abs(actual - expected))))
assert max_error == 0.0
source_path = path.with_name('model_quantized.onnx')
source = onnx.load(source_path, load_external_data=False)
assert list(source.graph.initializer) == list(model.graph.initializer[:-3])
assert [n for n in source.graph.node if n.op_type != 'GatherBlockQuantized'] == [n for n in model.graph.node if not n.name.startswith('edge_wasm_gather_0_')]
diagnostics = []
for check_path in (source_path, path):
    try:
        onnx.checker.check_model(str(check_path))
        diagnostics.append(None)
    except onnx.checker.ValidationError as error:
        diagnostics.append(str(error))
assert diagnostics[0] == diagnostics[1], 'Patched graph introduced a new checker failure'
report = {'seed': 20261029, 'randomTrials': 20, 'elementsPerTrial': 4096,
    'maxAbsoluteError': max_error, 'loweredSubgraphOnnxChecker': 'passed',
    'otherSourceNodesAndWeightsUnchanged': True,
    'fullGraphCheckerDiagnosticUnchangedFromUpstream': diagnostics[0],
    'patchedGraphSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    'note': 'Synthetic operator equivalence only; browser benchmark separately tests actual embeddings.'}
(root / 'artifacts/wasm-graph-validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
