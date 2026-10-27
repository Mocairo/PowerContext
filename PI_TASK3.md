# OceanBase 第三题：Pi 会话分支采集与断点恢复

本实现基于 PowerContext v1.2.0 冻结提交 `9db1753434162c0aa7c9f1189a1722e617880a52` 和赛事分支提交 `dab240063bfcfa7917898dc44da4baadb3747bd0`，保留第二题 CSV 实现及其提交历史。

提交仓库为 `https://github.com/Mocairo/powercontext.git`，实现分支为 `pi-session-history`。提交时填写 `git rev-parse HEAD` 输出的完整 40 位 SHA，并核对远程分支指向同一提交。赛事仓库将本题列为 20 分、全部必测通过才得分；公开测试不能代替隐藏评测成绩。

## 实现

产品代码仅新增两个文件，没有增加依赖或修改现有 Runtime、CSV 实现和官方测试：

- `src/powercontext/sources/pi_session.py`：规定的公共接口、CAPTURED Source、标准文本 projection、增量 connector。
- `src/powercontext/sources/_pi_session_parser.py`：UTF-8/BOM JSONL 解码、完整图校验及原始事件正文生成。

公共接口可直接导入：

```python
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
```

Source 定义为 `pi-event/1`，connector 为 `pi-session-history/1`。

### 解析与证据身份

先解析整个输入并校验所有节点的唯一 ID、显式 parentId 和父节点出现顺序，再沿指定 leaf 的 parentId 链逆向收集并按根到叶输出。未指定 leaf 时选择最后一个节点，包括控制节点。父节点只能为 null 或此前节点，因此前向引用和循环均被拒绝。空白 JSONL 行被忽略；仅含合法 session header 的文件返回空元组。

控制节点、没有可见正文的节点仍参与祖先链。compaction、branch_summary 和 context_edit 等节点不会删除、改写此前原始事件。正文保留原始空白和换行；工具参数使用保留 Unicode、排序键、无额外空格的 JSON。thinking/image 块不出现在文本中，但完整原始 record 仍保留。

Source 名称为 `pi-event:` 加 `SHA256(JSON([session_id, event_id]))`，总长 73 字符。对两个身份分量分别编码，避免分隔符拼接、路径字符、Unicode、长 ID 截断造成的身份混淆；身份与路径、leaf、正文和采集时间无关。

Source 保存完整正文、身份字段及 `record_json`。原始记录采用 JSON 字符串封装，是为了避免底层 RFC 8785 对大整数范围以及 `1.0`、`-0.0` 的规范化影响；`registry.read(source)` 还原完整 `PiEventInput`。这里的原始记录指 JSON 对象和字段值，不承诺保留 JSONL 字节排版、键顺序或转义写法。

标准 `powercontext.text-evidence/1` projection 包含完整 content，以及 session_id、event_id、parent_id、event_type、role。持久化 observation 后，可由原生 Source class 的 `model_validate(observation.payload)` 还原 Source，再调用 registry.read。

### 增量、失败及恢复

使用现有 `ConnectorLifecycle` 的 session.submit 提交，每个可见事件经 Runtime ingestion 写入。connector 不直接写数据库，也不自行提交 checkpoint。

checkpoint 包含格式版本、session ID、累计已导入事件 ID，以及已采集路径上原始记录的 SHA256，包括控制祖先。每次运行都先校验全部已保存路径的记录，即使某条路径当前未被选中；删除、改写记录或改接控制祖先均在提交新事件前报 ValueError。JSON 字段顺序和无语义排版变化不属于历史改写。单个 binding 绑定一个 session，替换 session ID 应使用另一个 binding。

选中分支仅提交尚未导入的可见事件，其他分支的 Source 和累计进度保留。所有提交成功后才返回完整 checkpoint，由 ConnectorLifecycle 原子比较并提交。拒收或异常保留旧 checkpoint。

Source 入库和 checkpoint 是不同事务：中途失败可能留下已经持久化的前缀证据。修复后从旧断点重试，Runtime 对相同身份、相同原文幂等返回，不增加 journal 条目；不同原文会被拒绝。connector 将这种未提交断点前缀的 Source 冲突转换为 ValueError；直接调用 Runtime ingestion 仍保持原有 SourceConflictError 合同。进度全部持久化，没有模块级状态。

解析时间/空间与输入和所选分支线性相关。每次增量仍完整读取输入并检查已保存记录，这是检测历史改写所需的工作；没有声称追加采集为常数时间。

## 验证与复现

宿主为 Linux x86_64、CPython 3.12.7。已逐项核对 `competition/requirements.txt` 的 78 项固定依赖。测试只使用临时 SQLite、固定本地数据与确定性的 Memory pipeline，不需要网络或在线模型。产品不会执行会话中的工具命令。

在产品仓库根目录运行：

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-pi
make -C competition test-csv

PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -q \
  tests/sources/test_pi_session_parser.py tests/e2e/test_pi_session_ingestion.py

PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -q \
  -p pytest_asyncio.plugin -o asyncio_mode=auto \
  tests/sources/test_csv_rows_competition.py \
  tests/test_sources.py tests/test_connectors.py tests/test_source_observations.py \
  tests/builtin/persistence/test_sources.py tests/builtin/persistence/test_connectors.py
```

最终产品代码的实测结果：

| 检查 | 实际结果 | 覆盖内容 |
| --- | --- | --- |
| check-baseline | 通过 | 指定冻结提交位于历史中 |
| 第三题官方 test-pi | 15 passed，4.90 s | 公开样例、正文、分支、持久化、增量 |
| 第三题补充测试 | 53 passed，28.42 s | 34 项解析 + 19 项 Runtime 链路测试 |
| 第二题官方 test-csv | 17 passed，2.36 s | CSV 公共合同兼容性 |
| CSV 与 Source/Connector 回归 | 81 passed，5.98 s | 53 项 CSV 补充 + 28 项既有相关回归 |
| 额外受限容器 | Pi 68 passed；兼容性 98 passed | 官方与补充测试，共 166 项 |

验收覆盖对应关系：

| 合同 | 测试证据 |
| --- | --- |
| 完整分支、默认/显式 leaf、控制节点、摘要前原始记录 | 官方测试；`test_controls_and_empty_events_connect_branches_and_default_last_node`；随机森林；5,000 节点链 |
| 各类正文、Unicode/BOM/CRLF、非法编码/JSON/图结构 | 官方测试与 `test_pi_session_parser.py` |
| Source identity、CAPTURED、完整 raw record、标准 metadata | `test_source_identity_components_and_immutable_raw_record`；官方 projection 检查 |
| 每个事件精确检索引用、重启后完整回读 | `test_each_retrieved_citation_reads_exact_full_source_including_large_tool_result`，全部 6 个事件逐条断言 |
| 大工具结果 | 500,074 UTF-8 字节的完整事件持久化和回读；Memory 仅作受限检索材料 |
| 分支切换、去重、追加、真实进程重启 | `test_actual_process_restart_branch_switch_append_and_replay`，5 个独立 Python 进程 |
| 部分写入、提交前后异常、拒收、checkpoint 保存失败 | `test_durable_sources_survive_failed_run_and_retry_without_duplicate_journal`，实际 Runtime 边界注入故障 |
| 损坏后修复、其他分支记录改写、删除和祖先改接 | `test_whole_archive_validation_precedes_new_ingestion_and_repair_resumes` 等恢复测试 |
| 已入库但未提交 checkpoint 的事件不可覆盖 | `test_changed_partially_persisted_event_cannot_overwrite_before_checkpoint_recovery` |

额外容器使用固定 Debian Python 镜像 `python@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286`，只读挂载同一份固定依赖及源码，配置 1 CPU、2 GiB、非 root、关闭网络。Pi 68 项耗时 30.37 s，兼容性 98 项耗时 6.38 s。容器前后源码 SHA256 一致。该附加环境不是官方 CentOS 基础镜像，也不是评测平台。运行脚本及完整 Docker 参数在随交付证据中的 `validate_container.py` 和 `container-results.json`。

新增 Python 文件通过 Ruff 0.15.7 检查/格式检查及 ty 0.0.56 类型检查，`uv lock --check --offline` 和 Git whitespace 检查通过。完整 `uv run --locked prek run -a` 在临时独立 checkout 中执行，以保护官方资产；该全仓库检查未通过，原因是既有赛事样例/测试格式问题及基线类型诊断。候选代码未引入新增类型诊断。官方资产在产品工作区保持原样，未为消除这些已有问题而修改。

## 实测优化

cProfile 显示初始实现对已解码的原始 JSON 重复进行 Pydantic 递归校验/复制，占 10,000 事件解析累计时间约 41%。优化仅在 JSON 解码、图结构和正文类型验证完成后，用 `PiEventInput.model_construct` 构造输出，保留原始 record，避免重复复制。公共 PiEventInput 本身仍是正常 Pydantic 模型。

50,000 可见事件、35,764,413 输入字节，固定同一 CPU，前后版本交替运行，每版 6 个独立进程；每次逐条核对完整 record、正文和身份：

| 指标中位数 | 优化前 | 最终实现 | 降低 |
| --- | --- | --- | --- |
| 解析时间 | 2.003349 s | 1.269949 s | 36.61% |
| 进程峰值 RSS | 437,900 KiB | 373,900 KiB | 14.62% |

这些数字是宿主解析基准，不是 Runtime 入库吞吐、平台时限或官方得分。10,000 事件样本受到主机调度变化影响，保留原始数据但不以它作为主要提速结论。

当前版本单次复现：

```bash
PYTHONPATH=src competition/.venv/bin/python scripts/benchmark_pi_session.py --events 50000
```

交付证据保留 `parser-before.py`、`parser-after.py`、`compare_parser.py`、逐次 JSONL、汇总和 cProfile 文件。将证据放在产品仓库同级的 `task3-evidence/` 后，可在产品根目录运行 `competition/.venv/bin/python ../task3-evidence/compare_parser.py` 重做前后比较。优化后已重新运行全部第三题公开与补充测试及受限容器验证。

## 验证边界

- 没有运行官方隐藏测试或提交评分，不能宣称已获得 20 分。
- 沿用基线整个 Source observation 最大 4 MiB 的协议限制；这不同于 Memory 单条 8192 字节限制。正文、record 和 projection 会共同占用 observation 空间，原始正文可用上限因内容而异。超限时 Runtime 明确拒绝，Source 不截断、旧 checkpoint 不推进；此行为已实测。没有验证超过该协议上限的事件能入库。
- 真实持久化验证使用 SQLite。未验证 OceanBase/seekdb 后端、物理断电、磁盘损坏或多进程同时采集同一 binding；现有 checkpoint 比较提交机制未改动。
- 故障测试通过真实 Runtime 操作边界注入异常，不能等同于物理存储故障。大规模性能数据仅覆盖解析。
- 官方验收只加载产品 `src/`；补充测试、benchmark、本文与交付证据均不参与评分逻辑。官方 `competition/` 文件及第二题源码与原提交一致。
