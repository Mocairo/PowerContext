# 浏览器实测结果

原始数据：`artifacts/submission-benchmark.json`
SHA-256：`d65ba29a027fab95e25530161fdf2a933c2d7611867faba6e497fbb51b7e989c`

环境：Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36; 28 logical CPUs; crossOriginIsolated=true

seekdb：5.7.25-OceanBase seekdb-v1.4.0.0。语料 24 篇，查询 12 条，每档 3 轮。

所有时间来自提交应用的 Worker/API/结果绘制路径。语义检索与混合检索分别评分，避免全文检索掩盖 embedding 变化。此数据集是小型合成验收集，不能代表公开标准榜单。

## gemma2-wasm-fp32-b1-refresh-each

实际后端 wasm，精度 fp32，批次 1，模型与分词器实际总文件 1,116,768,801 字节。

- 模型加载与预热：9721.22 ms；文档向量化吞吐：5.00 片段/s。
- 写入：2710.25 ms；索引刷新：16317.34 ms。
- 混合检索：数据库中位数 89.94 ms；端到端中位数 166.50 ms，p95 185.72 ms。
- 语义检索 Recall@5=0.958333，nDCG@5=0.985588。
- 混合检索 Recall@5=0.958333，nDCG@5=0.987512。

## gemma2-wasm-q8-b1-refresh-each

实际后端 wasm，精度 q8，批次 1，模型与分词器实际总文件 346,398,301 字节。

- 模型加载与预热：1013.33 ms；文档向量化吞吐：3.67 片段/s。
- 写入：2651.59 ms；索引刷新：15636.01 ms。
- 混合检索：数据库中位数 90.40 ms；端到端中位数 286.94 ms，p95 320.86 ms。
- 语义检索 Recall@5=0.958333，nDCG@5=0.985588。
- 混合检索 Recall@5=1.000000，nDCG@5=0.992215。

## gemma2-wasm-q8-b4-refresh-once

实际后端 wasm，精度 q8，批次 4，模型与分词器实际总文件 346,398,301 字节。

- 模型加载与预热：1113.99 ms；文档向量化吞吐：6.76 片段/s。
- 写入：1534.84 ms；索引刷新：604.21 ms。
- 混合检索：数据库中位数 89.98 ms；端到端中位数 216.43 ms，p95 233.39 ms。
- 语义检索 Recall@5=0.958333，nDCG@5=0.985588。
- 混合检索 Recall@5=1.000000，nDCG@5=0.992215。

## gemma2-webgpu-q8-b4-refresh-once

实际后端 webgpu，精度 q8，批次 4，模型与分词器实际总文件 346,397,233 字节。

- 模型加载与预热：1252.53 ms；文档向量化吞吐：20.23 片段/s。
- 写入：3106.18 ms；索引刷新：692.93 ms。
- 混合检索：数据库中位数 89.62 ms；端到端中位数 149.92 ms，p95 166.69 ms。
- 语义检索 Recall@5=0.958333，nDCG@5=0.985588。
- 混合检索 Recall@5=1.000000，nDCG@5=0.992215。

## 对同模型 FP32 基线的质量差异

预先冻结门槛：Recall@5 与 nDCG@5 的绝对下降各不超过 0.02。

- gemma2-wasm-q8-b1-refresh-each：{"vector":{"recallDrop":0,"ndcgDrop":0},"hybrid":{"recallDrop":-0.04166666666666663,"ndcgDrop":-0.004703190087156028}}；通过。
- gemma2-wasm-q8-b4-refresh-once：{"vector":{"recallDrop":0,"ndcgDrop":0},"hybrid":{"recallDrop":-0.04166666666666663,"ndcgDrop":-0.004703190087156028}}；通过。
- gemma2-webgpu-q8-b4-refresh-once：{"vector":{"recallDrop":0,"ndcgDrop":0},"hybrid":{"recallDrop":-0.04166666666666663,"ndcgDrop":-0.004703190087156028}}；通过。
