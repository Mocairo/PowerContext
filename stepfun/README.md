# Edge Atlas · 浏览器本地知识库

基于比赛分支 `2026-competition-ecosystem` 的 `d647275c308e09b7b623f322a26f5c4e85734fb0` 开发，项目资产均在 `stepfun/`。默认使用 **Google EmbeddingGemma 2 的文本编码器 + INT8 + WebGPU**，自动降级到 **WASM + INT8**。备选轻量中文模型为 BGE Small。文档和查询的向量计算全部发生在浏览器 Worker；Node 端通过官方 **seekdb-js (`seekdb`) 1.4.0** 保存向量、原文与元数据，并让 seekdb 执行全文与向量融合排序。

## 快速启动

需要 Node.js 22.12+，Chrome / Edge 最新稳定版，Docker Compose。Windows 可以使用 Docker Desktop；只有 WSL 中安装 Docker 时，脚本会自动调用 WSL 的 Docker。数据库默认仅绑定本机 `127.0.0.1:2889`，应用绑定 `127.0.0.1:4177`。

从本仓库的 `stepfun/` 执行；PowerShell 若禁用 npm.ps1，使用 `npm.cmd`：

```sh
npm ci
npm run db:up
npm run models:download
npm run dev
```

打开 <http://127.0.0.1:4177>。点击「载入示例语料」，会自动新建知识库并在浏览器生成全部向量；也可以先新建知识库，再导入 `.txt`、`.md`、`.jsonl`，或粘贴文字。JSONL 每行示例：`{"title":"笔记","text":"正文"}`。文件采用 UTF-8，单批最多 20 MB。

模型准备命令只下载公开模型与校验文件，**不进行任何 embedding 计算**。下载后的图、外部权重、分词器逐文件校验字节数；大文件进一步对照 Hugging Face LFS SHA-256。浏览器只从本应用 `/models/` 与 `/ort/` 读取文件，运行时禁止远程模型回退；完成准备后无需连接 Hugging Face。没有安装服务端 embedding provider，也不接受 `queryTexts` 自动向量化。

`npm run db:down` 停止本项目数据库并保留具名卷，下次启动可以继续使用。不要手动删除卷，除非你确实要丢弃知识库。现有数据库可复制 `.env.example` 为 `.env` 后配置连接。`SEEKDB_PATH` 可选启用 SDK 嵌入式模式；**本次实测是 server 模式，未验证嵌入式路径**。

生产构建启动：

```sh
npm run build
npm start
```

`public/models/` 和 `public/ort/` 在源码 checkout 中保留，由同一 Node 服务直接托管，不复制进 `dist/`；迁移机器时重新下载模型或连同 public 目录迁移。

## 模型与量化复现

- 默认原模型：[google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2)，Apache-2.0。ONNX 转换：[onnx-community/embeddinggemma-2-ONNX](https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/tree/daa72c51243991dfcaf9f9137d2c573d8f7790c0)，锁定 revision `daa72c51243991dfcaf9f9137d2c573d8f7790c0`。
- 只加载 270M 参数文本编码器，加载前把 `vision_config` 和 `audio_config` 设为 `null`，不下载视觉和音频权重。查询前缀 `task: search result | query: `；文档格式 `title: 标题 | text: 正文`。使用导出图内含的 mean pooling、投影与 768 维输出，再做 L2 归一化。输入上限固定 512 token，文本先按 380 个 Unicode 码点、60 字重叠切片；极端分词情况仍可能截断，质量评估必须使用同样规则。
- INT8 使用同 revision 发布的 `model_quantized.onnx` 及 `model_quantized.onnx_data`，Transformers.js `dtype: 'q8'`，并保留默认 FP32 输出向量。这是使用已发布的量化导出工件，**本项目没有训练或重新量化原模型**。SHA-256 与实际总体积保存在本地模型 `manifest.json`，基准原始文件包含此清单。INT8 表示图中的量化权重/算子方案，不代表所有算子和激活都是 INT8。
- 原始 INT8 图的 `GatherBlockQuantized` 在当前 WASM 构建不受支持。下载脚本自动生成 `model_wasm_quantized.onnx`：保持全部原始权重不变，将该节点拆成标准 ONNX Gather/Cast/Reshape/Sub/Mul，逐块执行 `(uint8_weight - uint8_zero_point) * float32_scale`。它只反量化本批选中的 token 行，复用相同的外部权重文件。WebGPU 使用原图，WASM 使用等价图，**不切换 embedding 模型**。固定随机种子、20 组共 81,920 个元素的 ONNX 子图运行与 NumPy 方程对照，最大绝对误差为 0；证据在 `artifacts/wasm-graph-validation.json`。完整原始图和转换图对 ONNX 1.20.1 checker 均有相同的上游 `SimplifiedLayerNormalization` schema 诊断，不能声称完整 checker 通过；两条图的真实浏览器运行另行验证。
- FP32 基线使用同版本的 `model.onnx` 及 `model.onnx_data`。统计口径为图 + 外部数据 + 共同分词器/配置；同时可从清单中单独统计权重。压缩比必须按实际字节相除，不能把整个模型仓库所有精度的下载总量当成一次加载量。
- [Google 官方卡片](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2) 对原模型 FP16 的数值范围提出警告，而 ONNX 转换卡片展示其 FP16 导出结果。为避开这个需要额外证明的差异，本项目对 Gemma 2 仅启用 INT8/FP32。不得将 BF16 标成赛题的 FP16。
- 备选：[Xenova/bge-small-zh-v1.5](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)，512 维、CLS pooling、查询添加中文检索前缀；WebGPU 默认 FP16，WASM 默认 INT8。两个模型各用独立知识库，禁止混写向量。

下载全部基线/优化工件：

```sh
npm run models:download -- --model=gemma2 --precisions=q8,fp32
npm run models:download -- --model=bge --precisions=fp32,fp16,q8
```

Gemma 2 INT8 总文件约 346.4 MB，首次下载比 BGE 大。FP32 基线约 1.12 GB；它仅为对照评测所需。具体数据见模型清单与实测记录。

## 数据层、增量与状态

`server/database.ts` 使用 `SeekdbClient.createCollection`，显式设置 `Schema`：IK smart 全文索引、HNSW cosine 向量索引、模型对应维度、`embeddingFunction: null`。每次重开集合也传 `null`，阻止 SDK 尝试默认服务端 embedding。SDK 负责实际集合表名与 `_metadata`、`document`、`embedding` 等列，应用不假定其物理表名。

每个知识库使用随机 `kb_<uuid>` 集合；`edge_kb_registry` 在同一 seekdb 中保存名称、模型 revision/维度/前缀规则指纹、写入版本与索引版本。浏览器 localStorage 只保存当前集合 ID。文档 ID 是 `SHA256(来源名, 切片序号, 原文内容)`，重复导入相同内容是幂等 upsert。**增量采用追加语义**：同名文件内容改变会形成新内容版本，并保留旧内容；它不是文件同步器。

每次最多 64 个片段/12 MB 请求，API 校验维度、有限值和归一化。单批 upsert 和写入版本更新在数据库事务中提交，导入的所有批次结束后统一执行 `CALL dbms_index_manager.refresh()`。刷新返回后更新索引版本，未刷新知识库查询返回 409，界面可手动重试。若中途失败，已提交批次保留，用户可重导入后刷新；不会将整个导入误报为原子操作。当前实现针对单 Node 进程，串行队列覆盖 SDK 完整操作，防止同连接会话变量互相覆盖；**扩展到多 Node 进程需增加数据库级协调**。

混合检索直接调用 `collection.hybridSearch({ query: { whereDocument: { $contains: query } }, knn: { queryEmbeddings: [vector] }, rank: { rrf: { rankConstant: 60, rankWindowSize: 20 } } })`。SDK 用 `DBMS_HYBRID_SEARCH.GET_SQL` 生成 SQL 并交回 seekdb 执行；应用按数据库返回顺序展示。没有浏览器搜索引擎、没有客户端两路合并，没有其他数据库。语义检索模式同样调用 seekdb 的向量查询。

## 测试与原始证据

```sh
npm test
npm run test:integration
```

单元测试覆盖 Unicode 切片、稳定 ID、指标公式以及会话队列失败恢复。真实 seekdb 集成测试覆盖未刷新阻止检索、增量立即可见、幂等导入、跨集合并发隔离、模型/维度校验和新连接恢复。集成测试使用**明确标注的合成单位向量，仅验证数据库合同**，不能作为浏览器推理或检索质量的证据。

浏览器真实评测入口：<http://127.0.0.1:4177/?benchmark=1>。选择模型后点「运行完整对比矩阵」，运行应用自身 Worker、写入、刷新与结果渲染路径；测试集合自动创建并清理，不改用户知识库。矩阵比较 WASM FP32 batch1/逐批刷新、WASM INT8 batch1/逐批刷新、WASM INT8 batch4/统一刷新、WebGPU INT8 batch4/统一刷新；BGE 另外比较同 WebGPU 的 FP32/FP16。

前两档隔离量化精度的变化；INT8 后两档隔离后端变化。batch1/逐批刷新到 batch4/统一刷新是**组合优化**，不能从该比较分别归因批处理和刷新策略各自的收益。加载时间只有在相同浏览器缓存状态下才可比较；原始记录给出 cold/warm 标记，不能把缓存命中当作量化的收益。

固定 `data/evaluation.json` 的 24 篇中英文短文和 12 条分级查询，不根据精度调整相关性。每档在预热后跑三轮混合检索，同时第一轮单独评估语义检索，避免全文搜索掩盖向量质量变化。记录所有结果 ID、query embedding、数据库、HTTP 往返、绘制后的端到端时间，报告中位数和 p95。质量门槛在测试前固定为相对 FP32 的 Recall@5、nDCG@5 **绝对下降均不超过 0.02**。小型合成集只证明基础功能与本组查询的质量，不代表标准榜单或通用检索性能。

「验证自动降级」向正常自动选择分支注入 adapter 不可用条件，再执行真实 WASM 向量化和检索。报告明确标为故障注入；强制 WASM 矩阵也会真实执行 CPU 路径。它们**不等于在第二台物理无 GPU 设备测试**。GPU 严格配置失败会记为失败，不会把降级后的 WASM 数据标成 GPU。

原始报告保存为 `artifacts/benchmark-*.json`。复算并生成可审计摘要：

```sh
npm run benchmark -- artifacts/benchmark-实际时间.json
```

最终实测摘要与限制见 `artifacts/results.md`、`TECHNICAL-NOTE.md`。模型加载记录包含文件读取、会话创建与预热；它不包含命令行提前下载模型的网络时间，不应把该数值称作公网首次下载耗时。测试是在同一浏览器实例依次执行，不视为随机交叉实验；冷/缓存加载与预热查询分别记录，硬件条件需随报告一起保留。

提交用的**一页技术说明 PDF**：[`output/pdf/technical-note.pdf`](output/pdf/technical-note.pdf)，已验证为 1 页并渲染检查，SHA-256：`dc5c226204849451ad41901f747293e094d39fb569bbc8be8762b49c9621d96d`。Markdown 源稿为 `TECHNICAL-NOTE.md`，可用 `scripts/technical-note.py` 重建（额外需要 Python、reportlab、pypdf 与中文 TTF/TTC 字体）；应用运行不需要 Python。

## 目录

- `src/`：页面、模型 Worker、浏览器实验台。
- `shared/`：固定模型合同、切片与检索指标。
- `server/`：仅存储/检索的 Node API、seekdb 集合和状态管理。
- `scripts/`：可校验模型下载、跨 Windows/WSL 的数据库启动、集成验证与结果复算。
- `data/`：固定语料与相关性标注；`artifacts/`：可追溯验证结果。

本项目不包含登录鉴权，面向本机个人知识库。应用与数据库均默认绑定 loopback；部署到可被他人访问的地址前需要另行实现鉴权，模型推理仍须在用户浏览器执行。
