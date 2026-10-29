# Edge Atlas：EmbeddingGemma 2 端侧知识库

**提交位置与流程。** 基于 `2026-competition-ecosystem` 的 `d647275c`，资产均在 `stepfun/`。Chrome/Edge 打开本地 Web 应用，文本导入 → 浏览器 Worker 向量化 → Node 接收已算向量 → seekdb 保存与混合检索 → 展示数据库排序及原文。无云端推理 API，无服务端 embedding 或结果回放。`npm ci && npm run db:up && npm run models:download && npm run dev`，入口 `http://127.0.0.1:4177`；Windows 使用 `npm.cmd`，Docker 在 WSL 中时脚本自动转交。

**模型与量化。** 原模型 [google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2)，Apache-2.0；采用 [ONNX Community 导出](https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/tree/daa72c51243991dfcaf9f9137d2c573d8f7790c0)，固定 revision `daa72c51243991dfcaf9f9137d2c573d8f7790c0`，Transformers.js **4.3.2** / ORT Web **1.31.0-dev.20260914-8d85527a0**。移除视觉/音频配置，仅加载 270M 文本编码器；使用指定 query/document 前缀、图内 mean pooling 和投影、768 维 L2 归一化输出。使用发布的 INT8 `model_quantized` 图，不重新训练；FP32 同版本作基线。按图+外部权重+相同配置/分词器口径：FP32 **1,116,768,801 B**，WebGPU INT8 **346,397,233 B**，WASM INT8 **346,398,301 B**，压缩比约 **3.22×**。文件逐一记录 SHA-256；下载脚本验证来源字节数和 LFS 哈希。

**后端与降级。** 显式 `device:'webgpu', dtype:'q8'`，adapter 申请、会话创建、预热和后续推理失败时自动转 WASM/INT8，UI 显示实际后端、精度及原因。原图的 `GatherBlockQuantized` 不受 WASM 支持，故生成等价标准 ONNX Gather/Cast/Sub/Mul/Reshape 子图，保留全部 INT8 权重并共享原权重文件，仅反量化选中 token 行；20 组/81,920 元素数值对照最大误差 **0**，其余节点/权重不变。真实自动降级及 WASM 导入检索通过。Gemma 2 不使用存在数值范围争议的 FP16；备选 BGE 的真实 FP16 不可用→WASM/INT8 路径也通过。

**seekdb 与状态。** seekdb-js **1.4.0** / seekdb **1.4.0.0**；每库独立集合，IK smart 全文索引、HNSW cosine 向量索引，显式禁用 SDK embedding function。registry 存模型指纹、写入/索引版本；稳定内容 ID 保证重复导入幂等，修改文件采用追加语义。批内 upsert 与版本更新在事务中提交，整次导入后统一 `dbms_index_manager.refresh()`；待刷新状态拒绝检索，可重试。`hybridSearch` 使用 `DBMS_HYBRID_SEARCH.GET_SQL`，由数据库执行全文+向量 RRF（常数 60，窗口≥20），无客户端融合。单进程队列防止 SDK 会话变量交叉；知识库切换丢弃旧结果。

**同机实测（2026-10-29）。** Windows 11、i7-14700KF、28 逻辑线程、32 GB 可见内存；机器显卡 GTX 1050 Ti，浏览器报告 NVIDIA/Pascal WebGPU adapter，不支持 shader-f16；Chromium 156，WASM 4 线程，seekdb Docker 4 CPU/2 GB。固定 24 篇合成中英文文档/12 条分级查询，预热后混合检索 3 轮，所有样本保留；端到端包含 query embedding、HTTP/DB 和结果绘制。

- FP32/WASM、batch1/逐批刷新：向量化 **5.00 片段/s**；DB 混合检索中位数 **89.94 ms**；端到端 **166.50 ms**（p95 **185.72 ms**）；混合 Recall@5 **0.9583**、nDCG@5 **0.9875**。
- INT8/WASM、batch4/统一刷新：**6.76 片段/s**；DB **89.98 ms**；端到端 **216.43 ms**（p95 **233.39 ms**）；混合 Recall@5 **1.0000**、nDCG@5 **0.9922**。
- INT8/WebGPU、batch4/统一刷新：**20.23 片段/s**；DB **89.62 ms**；端到端 **149.92 ms**（p95 **166.69 ms**）；混合 Recall@5 **1.0000**、nDCG@5 **0.9922**。

**质量与解释。** 所有精度/后端的独立语义检索 Recall@5 **0.9583**、nDCG@5 **0.9856**，均满足预先固定的绝对下降≤0.02 门槛。固定 INT8/batch4/刷新策略时，WebGPU 向量化吞吐为 WASM 的 **2.99×**，端到端中位数减少 **30.73%**。INT8/WASM 查询比 FP32 慢，不能声称量化必然加速。batch+刷新是组合优化，不分别归因。模型加载含文件读取/会话创建/预热，FP32 首次浏览器缓存 **9.72 s**，INT8 缓存加载 **1.01–1.25 s**；缓存状态不同，不将差值计为量化收益，也不当作公网下载时间。合成小评测集、顺序执行及单机结果不代表标准榜单；原生 Edge、物理无 GPU 设备和 embedded 模式未实测。

**复核入口。** `artifacts/submission-benchmark.json` 保留逐查询 ID/原始阶段时间/模型 SHA-256，`npm run benchmark -- artifacts/submission-benchmark.json` 独立复算指标和中位数；`artifacts/results.md` 为完整摘要。数据库 7 项真实验收、4 项单元测试、自动降级与算子等价证据均附于 `artifacts/`。浏览器复测入口 `/?benchmark=1`；完整启动、恢复及边界见 `README.md`。
