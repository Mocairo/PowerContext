# OceanBase 第 5、6、7 题累计交付

最终分支为 `solution-567`，仓库为 `https://github.com/Mocairo/powercontext.git`。它包含此前第二题 CSV、第三题 Pi 实现及本次第五、六、七题，保留完整提交历史。

冻结基线为 `9db1753434162c0aa7c9f1189a1722e617880a52`，赛事分支基线为 `dab240063bfcfa7917898dc44da4baadb3747bd0`，本次开发起点为 `92cf82494eea8bf179d0aa6a73ffc1191bc8d5a4`。本次没有修改官方题面、测试、样例、依赖锁定文件或此前 CSV/Pi 源码。

三题分别在工作区外的独立 worktree 开发，经主代理复查后整合，最终测试全部针对正式产品目录的同一份源码执行。源码哈希在测试前后核对一致。提交时以 `git rev-parse solution-567` 输出的完整 SHA 为准；远程可获取状态和表单填写值另见交付包 `README.md`。

## 第五题：HTTP 重排

主要文件：

- `src/powercontext/builtin/artifacts/memory/http_reranker.py`
- `tests/builtin/artifacts/memory/test_http_reranker.py`
- `tests/e2e/test_http_memory_reranker.py`

`HTTPMemoryReranker` 使用调用方提供的 `httpx.AsyncClient`，不自行创建或关闭客户端。按 batch_size 切分完整候选池，全局一基 rank 不重新编号，每个候选恰好发出一次。响应可以乱序，但必须精确覆盖本批 rank，且对象字段集合完全符合合同；重复 JSON 字段、布尔/浮点 rank、非数值或非有限 score 都被拒绝。

所有批次完成后统一按 score 降序、原始 rank 升序选择。数值整数保持原精度，不转换成 float 造成错误平分。已有 Memory 服务根据 selected_ranks 引用原候选，保持 Memory、entry、entry version、正文等身份信息。

非 2xx 和网络失败保留 httpx 异常，非法 JSON/schema 抛 ValueError。重定向不被隐式跟随，避免把非 2xx 当作成功或增加未报告的调用。正常完成时 usage.requests 等于实际 POST 次数。

补充验证包含实际本地 HTTP 服务、SQLite Runtime 关闭重开、逐条精确引用回读和 173 候选／batch 7 的 25 次请求。公开 SciFact 样本的 8 篇摘要／1 条 query 实测 batch 7 发出 2 次请求，请求 JSON 正文共 6,367 字节，响应 JSON 正文共 211 字节；此测量使用明确的合成分数，仅验证协议、覆盖与排序。

正式 173 文档／20 query 的固定向量与 qrels 不在公开包内，因此没有声称满足正式 Recall@10、MRR@10 门槛。

## 第六题：增量 Context Pack

主要文件：

- `src/powercontext/builtin/runtime/context_pack.py`
- `src/powercontext/builtin/runtime/application.py` 中的 `ScopedContextApplication.prepare_incremental`
- `tests/builtin/runtime/test_context_pack.py`
- `tests/e2e/test_context_pack_runtime.py`
- `scripts/benchmark_context_pack.py`

ContextPack 和 ContextPackDelta 为不可变 dataclass。完整包保存 bytes 元组，摘要与 byte_count 从内容计算。wire 使用标准 Base64、排序 JSON key 和无额外空白的编码；byte_count 通过固定点迭代计入该字段自身长度。解码检查完整字段集合、schema、重复键、编码、UTF-8、摘要、严格整数和实际规范 wire 大小。

`from_content` 默认以 512 bytes 为上限确定性分块，并回退到合法 UTF-8 字符边界。无法容纳一个字符时抛 ContextPackError。重组不改变原始正文。`between` 复用最长公共分块前缀；接收端也允许任何较短合法前缀，包括 0，并核对 base 和 target 摘要。首部、中部、尾部修改、缩短和空包均支持。

`prepare_incremental` 调用既有 prepare，转发授权回调；无 base 返回完整包，有 base 返回 delta。普通 prepare 的正文和 citation 语义保持不变，content=None 转为空文本。接收端只使用序列化后的 base 和 delta，补充测试包含独立 Python 接收进程，以及真实 Runtime 重启后逐条引用回读。

### 实际传输成本

测量包含首次完整包和后续全部协议开销，逐轮与普通 prepare 的 UTF-8 正文比较。gzip 对照每轮独立调用 `gzip.compress(compresslevel=9, mtime=0)`。

| 序列 | 原文累计 | 完整 v1 pack 累计 | gzip 累计 | 实际增量累计 | 对原文节省 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 官方公开两条 turn，连续两轮发生变化 | 1,938 B | 2,911 B | 1,131 B | 3,014 B | -55.52% |
| 原始 QMSum 8 条开始，本地声明的 40 轮序列 | 151,310 B | 209,230 B | 65,840 B | 30,448 B | 79.88% |

40 轮全部非空，共 4 种不同 Context，对完整包节省 85.45%，对 gzip 节省 53.75%。它超过 45% 数值门槛，但不是正式隐藏测量序列；不能据此宣称正式成本门已通过。连续修改的短序列可能因编码和校验开销增加传输量。

额外输入取自官方引用的固定 QMSum 文件 `data/ALL/test/ES2004a.json`，SHA256 为 `911e56db0d8482f1e730bae0a8fcf0ab0095c94abb000ba0516e975fa6e48b31`。选择其中含 control 的 11 条原始 turn，保留 speaker、全文与顺序，索引为 14、184、186、195、203、208、225、237、267、268、309。前 8 条先入库，第 10/20/30 轮各追加 1 条，四段 query 为 remote、remote、control、design。该数据和轮次仅用于测量脚本，没有写入产品算法。

```bash
PYTHONPATH=src competition/.venv/bin/python scripts/benchmark_context_pack.py \
  --turns-json ../task567-evidence/incremental-qmsum-turns.json \
  --queries remote remote control design

PYTHONPATH=src competition/.venv/bin/python scripts/benchmark_context_pack.py \
  --turns-json competition/challenges/context-02-incremental-pack/workload/public/turns.json \
  --initial-turns 1 --rounds 2 --append-every 1 --queries remote
```

原始来源、归一测量输入和每轮 SHA256/字节数随证据交付。新建数据库产生新的引用身份，gzip 字节数可能略变；正文和 wire 仍按相同规则实际计量。

## 第七题：数量触发

主要文件为 `src/powercontext/builtin/runtime/` 下的 `config.py`、`composition.py`、`processing_discovery.py`、`artifact_processing.py`、`processing_registry.py`；补充测试为 `tests/builtin/runtime/test_source_count_trigger.py`。

新增两个默认 None 的严格正整数配置 `memory_source_count_threshold`、`topic_memory_source_count_threshold`。拒绝布尔、字符串、浮点、零和负数；任意大正整数仍合法，计数结果在 Python 中比较，避免超出数据库绑定整数范围。

Supervisor 增加独立、有界的数量扫描，按 scope 和处理 binding 的成功 cursor 查询后续实际 Source journal 行数。仅读取计数所需元数据，不按正文长度、offset 差值或生成结果计数。相同身份重放、receipt 升级不增加行数；lineage-only 来源参与计数，其原有生成证据过滤保持不变。

数量通道与时间通道独立，满足任一条件即可接受原有处理请求。显式 flush 和已接受请求沿既有通道执行，不重新套用门槛。Memory 每次只处理原窗口，再按新 cursor 判断；Topic Memory 沿用已接受 wave 的后续分窗。请求持久化、失败重试、成功 cursor 提交和重启恢复均复用原有机制。

扫描按有界页推进，低于门槛的前页不会遮挡后续就绪 scope。数量轮询也覆盖无显式唤醒的 lineage 写入；未开启数量门槛时保留原有行为。扫描失败后的唤醒尊重退避时间，独立复查用实际 SQLite 证明已消除退避期间的毫秒级空转。

官方 17 项测试完整使用 45 秒自动处理等待上限、5 秒静默观察和真实本地 provider／worker。额外覆盖稀疏 journal、105 个未就绪 scope 后的就绪条目、接收事务回滚、任意大门槛、退避及恢复后提高/关闭门槛。

## 最终验证

主机为 Linux x86_64、CPython 3.12.7；官方环境的 78 项固定依赖逐项核对一致。测试使用本地服务、临时数据库和离线数据，不使用在线模型 Key。测试子进程清除了继承的代理变量，避免本地 HTTP fixture 被服务器全局 SOCKS 设置影响；没有修改固定依赖或账户配置。

在产品根目录安装／复现官方检查：

```bash
make -C competition install
# 若当前终端配置了全局代理，测试子进程应清除以下变量。
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
make -C competition check-baseline
make -C competition test-rerank
make -C competition test-incremental
make -C competition test-count-trigger
make -C competition test
```

| 最终实际命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `make -C competition check-baseline` | 0 | 冻结基线可达 |
| `make -C competition test-rerank` | 0 | 10 passed |
| `make -C competition test-incremental` | 0 | 14 passed |
| `make -C competition test-count-trigger` | 0 | 17 passed，命令墙钟 273.81 s |
| `make -C competition test` | 2 | 第二、三、五、六、七题共 73 项通过；其他未实现题目保留原有失败 |

全量测试中，第四题缺少 pdf_pages、第八题缺少 coalescing、第九题缺少 source_lifecycle、第十题缺少 evidence_selection、第十一题未实现单 SQL 混合检索。这五组的失败测试身份与开发前完全相同。此前第二题 17 项、第三题 15 项仍通过；没有将本次任务扩展到其他赛题。

最终整合源码还通过 391 项固定依赖下的产品补充／相关回归，以及 186 项锁定开发环境下的 CLI、组合、worker、Topic Memory 和持久化回归。后一组需要现有开发依赖中的 CLI 包；它们没有被添加到评测环境。完整命令、退出码、日志和源码哈希在交付证据中。

Ruff、变更文件类型检查、whitespace 和锁文件检查通过。完整 `uv run --locked prek run -a` 在临时独立 checkout 中执行，退出码 1：官方既有样例/测试格式问题保持原样；全库 ty 从 217 条降至 207 条，其中修复 12 条缺失接口提示，新显露 2 条位于未修改官方负例测试的非法参数类型提示。本次变更文件无类型诊断。没有通过改写官方测试或放宽严格配置类型消除这些提示。

## 验证范围与限制

- 未运行官方隐藏评测或操作平台“立即提测”，不能保证得分或宣称满分。
- 正式 SciFact 向量/qrels 和完整隐藏 40 轮序列不可获得；分别报告协议测量和本地序列成本，不替代正式质量验收。
- 数量调度已验证默认 all／global、单写入者 SQLite 和真实 worker。未验证 OceanBase/seekdb 数量调度、多写入者或评测平台的实际 CPU/内存配额。
- 关闭故意失败的 supervisor 时，压力探针偶见 SQLite `database is locked` 退出日志。独立基线对照也复现该已有行为：基线 9/60、候选 8/60；数据库关闭重开后的已接受请求恢复均为 60/60。没有改动无关通用关闭逻辑。
- 此前 Pi Source 的 4 MiB observation 协议上限保持不变；本次没有改变已有题目的能力边界。
- 当前 origin 匿名可读，实际为公开仓库。只读推送认证预检因缺少 HTTPS 凭据失败，没有执行远程写入；平台可获取性须以最终提交的远程核验结果为准。
