# OceanBase 第 8—11 题累计交付

仓库：<https://github.com/Mocairo/powercontext>；交付分支：`solution-8-11`。

本分支从 `ef7b3aa8868e6b1a223f0b8ce3be4c7e54bb269b`（`solution-567`）继续开发，保留完整历史和此前第 2、3、5、6、7 题实现。冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52` 可达。原 `powercontext-work` 未被切换或改写；本次在独立 worktree 开发，各子任务独立提交后由主代理整合、审查和验证。

四题产品实现均位于 `src/`。没有修改官方 `competition/` 题面、测试、样例、评分逻辑、固定依赖，或 `pyproject.toml`、`uv.lock`。语雀页面无法访问，实施依据为完整仓库题面；未发现可核实的网页契约冲突。

最终完整 commit SHA、推送状态与提测表单值见交付包 `task8-11-delivery/README.md`；本文件自身所在的提交可用 `git rev-parse solution-8-11` 核实。

## 第 8 题：Embedding 并发合并和缓存

主要实现：`src/powercontext/builtin/inference/coalescing.py`。

- 同一事件循环内先登记去重文本，再按合并窗口派发；排队和执行中的请求都可被后续调用复用。每批不超过 `max_batch_size`，返回时恢复每个调用的原始顺序和重复位置，空输入不调用 provider。
- 只缓存成功向量，使用有界 LRU，命中刷新顺序。容量为 0 时仍共享执行中的工作。数量不匹配产生 `ValueError`，失败批次不缓存且可重试，独立批次正常完成。
- 等待者通过 `asyncio.shield` 共享 Future；取消不传播给其他等待者，后台失败被收集，全部等待者离开也不会遗留未处理异常。
- 每个结果按其等待的不同批次汇总 provider 原有 requests、input_tokens、output_tokens，同批只计一次，纯缓存命中 requests=0。
- 保留底层 profile。支持既有非对称 `embed_query`：文档和查询使用不同键及批次，共享同一个有界缓存容量；底层不区分查询时，两者共享原队列及向量。

官方 6 scope × 24 条相同文本预算已真实执行：`max_batch_size=6, coalesce_ms=0, cache_size=48`，provider 批次大小为 `[6, 6, 6, 6]`，共 **4 次请求**、24 个不同文本。随后 6 次原生向量搜索均命中对应正文，累计仍为 4 次请求。每批 provider usage 为 requests=1、input_tokens=42、output_tokens=0。

补充测试：`tests/builtin/inference/test_coalescing.py`，包含晚到调用、缓存关闭、局部失败、全部等待者取消、provider 自身取消、usage、跨文档/查询缓存边界和真实非对称 Memory 搜索。

## 第 9 题：来源生命周期

主要实现：

- `src/powercontext/builtin/runtime/source_lifecycle.py`
- `src/powercontext/builtin/persistence/source_lifecycle.py`
- `src/powercontext/builtin/persistence/topic_memory.py`、`tables.py`
- `runtime/application.py`、`composition.py` 的 Runtime 注入。

事件严格校验类型、正整数、非空文本和额外字段；支持 scope 内注册 Source、Memory 和 Topic 引用，实际读取验证其存在性。节点身份冲突、缺失/重复父节点、自环和环均拒绝。依赖在注册时和事件后立即按多层 all/any 计算。

图、原始引用、完整事件及状态保存在数据库。事件 revision 精确匹配；sequence 按 Source 单调递增，同事件重放幂等，同序号不同事件或低序号拒绝。先持久化事件意图，再用原生 `MemoryService.forget/reactivate` 对齐 manifest 和索引；正文版本不重写。原生写入前或提交后发生中断，同事件重试按真实状态补齐，成功重放不增加 Memory revision。

Topic 的当前可用性保存在与图快照同事务更新的排除投影，原生搜索融合前与 browse 查询中使用它；历史 ArtifactRef 仍精确可读。generation 拒绝不可用节点，audit 返回原始注册引用。完整 reason 保留在事件中，原生 Memory 审计说明遵循既有 512 字符限制。

同一物理正文支持等价依赖的别名节点；矛盾依赖绑定明确拒绝，避免一个正文被同时要求处于两种原生状态。生命周期针对题面单写者、每 scope 16—20 节点及 4 个 Topic 的规模；没有宣称支持多写者改图或解决超出此规模的大量停用 Topic 挤占候选池问题。

补充测试：`tests/builtin/runtime/test_source_lifecycle.py`。覆盖严格字段、真实引用、scope 隔离、失败注册、撤回/恢复中断、提交后异常、重启重放、大整数和长 Unicode reason。

## 第 10 题：查询驱动的原文证据

主要实现：`runtime/evidence_selection.py`、`prepared_context.py`、`models.py` 和 `application.py`。

`PrepareContextRequest.evidence_selection` 是默认 False 的严格 bool。开启后，真实 Context 召回和 builder 使用 Memory 候选，以查询词覆盖、相邻词对、保守词形归一和通用文章结构选择原文句子；无空格中文使用字符及相邻词对评分；允许同一 entry 的多个片段，相邻片段可合并以节约 citation 开销。过长相关句子使用原文查询窗口，避免被短而无关的句子遮挡。算法不读取文档 ID、标注文件、评测答案，不生成或改写正文。

每个 fragment 保存 `MemoryEntryAddress`、Python 半开字符区间和精确原文切片。origins、fragments、JSON items 顺序和数量逐项对应；scope、Memory revision、entry ID、entry version ID 均保留。trust policy、envelope、JSON 转义、完整 citation 和正文一起按 UTF-8 实际渲染计费，`content_bytes` 等于完整输出字节数，并且不超过 `max_bytes`；不足以容纳合法内容时返回空结果。

普通 prepare 保持兼容，增量 Context Pack 可传输并重建该证据 JSON。证据选择仅扩展题面指定的 Python Runtime 接口，没有新增 HTTP OpenAPI 或 MCP 传输字段。

补充测试：`tests/builtin/runtime/test_evidence_selection.py`、`tests/e2e/test_evidence_selection_runtime.py`。额外独立验证 120 组 Unicode、JSON 转义、字符区间和预算不变量；这些合成检查不构成语义质量指标。

### 质量测量边界

正式 24 claim / 48 请求的选取及标注未公开，因此不能宣称已通过正式质量门。本地测量使用 SciFact 官方原始 corpus、train/dev，来源和 SHA256 保存在证据目录；产品调用只收到 query 与三个候选正文，不收到 gold 标注。

本地方法：选择只关联一篇标注文档的 claim，加入全库词项检索最强的两篇干扰文档，按查询分数排序候选；真实写入 Memory 后，独立检查全部标注区间连同实际 citation/envelope 能否装入 1400 B。可行请求分别在 1400 B 和 1800 B 下测量。所有 annotation groups 的区间取并集，按题面词频交集公式统计 token recall。该方法与正式隐藏 workload 不同，开发集结果曾用于诊断，因此不应描述成盲测。

最终集成源码在开发集检查了 178 个单证据文档 claim，其中 40 个不满足独立的 1400 B 可行性条件；其余 **138 claim × 2 档预算 = 276 次请求**。实际平均 evidence token recall 为 **0.6466726811**，完整覆盖率为 **53.2609%（147/276）**。所有请求均通过原文、精确引用及完整字节预算断言。

**这份本地扩展集的 token recall 略低于 0.65；不宣称第 10 题正式质量门已通过。** 完整覆盖比例超过 40% 也不能替代正式样本验收。最终指标文件为 `task8-11-evidence/final-evidence-quality.json`，保留逐请求区间、预算、原始输入 SHA 和产品源码 SHA。

原始 corpus SHA256 为 `b8d6c89624cb2ed74dee8938effc4f5d8bd2086887880af8110d64be4ceade62`，claims_dev 为 `86f0435d08fdb65d1aa41d1472684f57e6e71930626497bdf4d7a9ec1a632217`。

可复现脚本：

```bash
PYTHONPATH=src competition/.venv/bin/python scripts/benchmark_evidence_selection.py \
  --corpus /path/to/corpus.jsonl --claims /path/to/claims_dev.jsonl \
  --output /tmp/evidence-metrics.json
```

## 第 11 题：seekdb 单 SQL 混合候选

主要实现：`persistence/seekdb/memory_index.py`、`persistence/oceanbase/memory_index.py` 和 Runtime composition。

复用原 schema、AsyncConnection 和原生 FTS/vector 投影，用一条 `UNION ALL` statement 返回两路候选，并在数据库内执行 scope、Memory artifact、tag 条件及通道 limit。后续融合、精确正文读取和当前 head 校验沿原产品流程执行。旧 projection.head_revision 不会排除未改动的活跃条目，返回引用采用已经校验的当前 Memory head。

FTS-only、vector-only 保留原路径。追加、正文修订、停用和恢复沿原事务更新索引；每个命中保持有效版本、精确正文和 fts/vector 通道标识。

真实 seekdb 暴露了高选择性 tag 与小 top-k 的优化器边界：相关 tag 子查询可能在 ANN 提前截断后才执行。本实现复用既有完整性检查的有效行数，在 tag 模式使原生向量索引覆盖这些行，再在同一 SQL 内过滤、排序和限制结果。没有额外候选 statement 或 Python 距离计算；代价是 tag 模式的扫描工作可能随有效行数增长。

真实 `pylibseekdb==1.3.0.post4` 的执行计划包括：

```text
TEXT RETRIEVAL SCAN
VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)
```

完整 SQL、绑定参数和 EXPLAIN 保存在证据目录。计划中精确正文表可能使用 TABLE GET 或 TABLE FULL SCAN，这是数据库内正文连接，不是 Python 全表计算匹配/距离。

补充真实数据库测试：`tests/e2e/test_seekdb_hybrid.py`。覆盖 40 条候选、candidate_limit=1、tag all/any/无匹配、多 Memory、scope 隔离、版本与状态、全停用空结果、单通道及四份实际执行计划。

### 同库对照范围

对照使用冻结双查询候选路径；冻结基线至开发起点的 Memory service、CompositeMemoryIndex 和 OceanBase memory index 均无差异。候选和对照共用同库、同 scope/head/query/向量及身份，各预热后交替运行，完整 `Memory.search` 计时。

公开实验仅有 8 篇文档、1 条有 qrel 的 query，使用固定 384 维单位 L2 合成向量。3 scope × 20 轮得到 60 次测量：候选 statement 1 条，对照 2 条；完整结果 tuple 一致率 100%，双方 Recall@10=1、MRR@10=1/6。另用 8 篇原标题作为额外查询，9 query × 3 scope × 2 轮共 54 次，完整 tuple 一致率同为 100%；其中只有原 claim 的 6 次有相关性标注。

主代理在最终集成目录复测完整搜索 p50 为 **38.6812 ms**，同库对照为 **39.3107 ms**，全部 60 次均为单候选 SQL且结果完整 tuple 一致。原始记录为 `task8-11-evidence/final-hybrid-metrics.json`。

只有 8 篇文档时 Recall@10=1 很容易达到，合成向量也不具有正式语义质量；这些结果不能替代 173 文档 / 20 query / 60 次正式质量验收。最终完整搜索 p50 和原始记录见交付包；未在平台 1 CPU / 2 GiB 配额下复现，单 SQL 不等于已经证明端到端加速。

可复现脚本为 `scripts/benchmark_seekdb_hybrid.py`，参数说明使用 `--help`。

## 整合、测试和交付

主机为 Linux x86_64、Python 3.12.7。最终 worktree 按官方 `make -C competition install` 单独安装环境，78 项固定依赖逐项核对一致。所有测试使用临时数据库、离线模型和本地响应，不依赖在线模型 Key。测试子进程清除了继承的代理变量，未改账户配置。

`tests/e2e/test_lifecycle_evidence_embedding.py` 在 SQLite 和真实 seekdb 上贯穿四题，并覆盖既有第 5 题 HTTP reranker 协议和第 6 题 Context Pack：缓存向量不能使停用正文重新进入搜索、重排或 Context；重启和恢复保留正文版本与历史引用，增量重建逐字一致。

最终必须执行的命令：

```bash
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
make -C competition check-baseline
make -C competition test-embedding
make -C competition test-lifecycle
make -C competition test-evidence
make -C competition test-hybrid
make -C competition test
```

| 命令 / 检查 | 退出码 | 结果 |
| --- | ---: | --- |
| `make -C competition check-baseline` | 0 | 冻结基线可达 |
| `make -C competition test-embedding` | 0 | 8 passed |
| `make -C competition test-lifecycle` | 0 | 4 passed |
| `make -C competition test-evidence` | 0 | 3 passed |
| `make -C competition test-hybrid` | 0 | 1 passed，真实 seekdb |
| `make -C competition test` | 2 | 89 passed；仅原有第 4 题 PDF 导入错误 |
| 固定环境产品补充/相关回归 | 0 | 419 passed，42 个既有可选条件跳过 |
| 锁定开发环境 Topic/recall/processing 回归 | 0 | 93 passed |
| 四题整合 SQLite + seekdb | 0 | 2 passed |
| seekdb 多过滤/状态专项 | 0 | 1 passed |
| 最终 Context/证据/增量相关组 | 0 | 167 passed，与上述回归有重叠 |

每条命令的退出码、用例计数、时间、源码哈希和原始日志保存在 `task8-11-evidence/final-*-results.json`。全量公开测试中的第 4 题 `powercontext.sources.pdf_pages` 缺失是起始版本原有未实现项，本任务没有扩大为 PDF 实现。此前第 2、3、5、6、7 题继续回归。

产品补充回归的可选服务跳过项与比赛必测项分开记录：既有 OceanBase URL 条件和 `POWERCONTEXT_TEST_SEEKDB=1` 条件未全部启用；本次专门的 seekdb 官方、tag 与四题整合测试均使用真实数据库执行。

所有本次变更文件的 Ruff、类型检查及 whitespace 检查通过。全库 `uv run --locked prek run -a` 在隔离 checkout 执行，现有官方样例格式及历史类型问题仍使其非零退出；没有把格式化后的官方文件带回交付分支。全库 ty 从起点 207 条诊断变为 201 条，其中新显露的官方负例 `SourceLifecycleEvent(..., extra=True)` 参数诊断来自其预期拒绝额外字段的测试；本次变更文件没有类型诊断。

`origin` 经实际查询是公开 Fork。HTTPS 推送预检因缺少凭据失败（`could not read Username for 'https://github.com'`），本地提交和完整 Git bundle 已保留；平台“立即提测”仍由用户手动操作。远端未包含最终 commit 前，不能声称平台已可获取本版本。
