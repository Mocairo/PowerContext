# 第二题交付：CSV 行级采集与原始记录回查

## 提交内容

实现位于 `src/powercontext/sources/csv_rows.py`。新增标准库 CSV 解析、稳定行 identity、CAPTURED Source Definition、read 和标准 TextEvidence projection；沿用产品现有 manifest、observation、Runtime ingestion 和 MemoryCitation。

未修改官方 `competition/`、固定依赖或现有产品文件。未增加依赖、模型调用、网络调用、全局缓存或文件写入。辅助测试和 benchmark 不参与平台判题。

基于 `2026-competition` 的 `dab240063bfcfa7917898dc44da4baadb3747bd0`，保留冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52` 的完整祖先关系。

平台提交格式是**产品仓库 HTTPS URL、实际分支、完整 40 位 commit SHA**，不是上传单独的 Python 文件。已配置的仓库 URL 为 `https://github.com/Mocairo/powercontext.git`；交付的独立分支为 `codex/csv-row-evidence`。以交付目录的 `submission.json` 为准，确认 `remote_verified` 后再向平台填写其中的 SHA。本地 commit 尚未推送时，平台无法获取。

## 规则依据与差异

已完整读取[指定题面](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/yt9ctvr4n9lc0p1l)、[统一算分规则](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/xkmkblbzlzk581ya)、相关参赛指南及[官方赛事分支](https://github.com/oceanbase/powercontext/tree/2026-competition/competition)。核验日期：2026-10-27。

- 官方统一算分表和当前赛事仓库为 **20 分，全通过得分，任一必测失败为 0 分**。语雀单题页仍写 10 分；此差异已记录，没有把本地测试换算成官方成绩。
- 没有解析速度、吞吐率的额外加分公式；排名按分数优先，同分按完成时间先后。
- 产品实现仅从 `src/` 加载，源码只读，不得增加依赖或改动评测测试、数据、评分逻辑。
- 统一公开环境：Linux x86_64、Python 3.12、固定依赖；容器 1 CPU、2 GiB、512 pids，运行时断网、普通用户。
- 未找到本题独立运行秒数上限或最大输入规模。“无排队约 15 分钟”是反馈时效，不能当作单题执行超时。
- GitHub 指南要求私有仓库，而当前仓库说明允许已有公开 Fork；未变更仓库可见性。平台读取权限和实际官方验收仍需在参赛账户下确认。

## 实现与边界

使用 `csv.reader(strict=True)`，保持逗号、双引号及双引号转义语义。`TextIOWrapper` 严格 UTF-8 解码并接受开头 BOM，`newline=""` 保留字段内 CR/LF/CRLF。只忽略表头之后的真正空记录；空字段、空白字段不视作空记录。

表头非空、非纯空白且精确唯一；主键精确匹配原列名。字段和值不 trim、不转换类型。整份输入完成宽度、主键非空/唯一、identity 长度及编码校验后才返回 tuple；错误统一满足 `ValueError` 契约。

identity 的两个组件分别调用 `quote(..., safe="")`，长度不超过 256。只有实际存在数据行时才编码文档前缀，因此只有表头的文档不会错误触发行 identity 校验。JSON projection 保持原列序、完整字符串以及规定的五个 metadata 键。

解析路径先验证全部行约束，再用 `model_construct` 构造类型已经确定的行；列名、字段均来自标准 CSV reader，文档字段已由输入模型验证。不可变表头 tuple 可安全复用。公开 resolve 路径仍验证直接构造的行输入。独立检查确认构造结果与正常 Pydantic 重校验、序列化一致。

## 正确性与环境验证

| 验证 | 本地结果 |
| --- | --- |
| 原始仓库基线 | 缺少 `csv_rows`，公开测试 collection 失败，未通过 |
| 初始可运行版本 | 17 项公开测试全部通过 |
| 最终公开测试 | 17 项全部通过 |
| 最终补充边界及跨进程持久化测试 | 53 项全部通过 |
| 既有 Source / observation 回归 | 12 项全部通过 |
| 独立短 CSV 穷举参考对照 | 9,331 项全部一致 |
| 确定性随机 CSV | 2,048 行字段往返一致 |
| 固定依赖及基线 | 78 项固定依赖精确匹配；祖先检查通过 |
| 静态检查 | Ruff 0.15.7 lint/format、仓库锁定 ty 0.0.56 定向检查通过 |

补充测试覆盖 BOM、严格 UTF-8 错误、CR/LF/CRLF、多行引号、空记录与 quoted-empty 的区别、Unicode/NUL、空白保真、列名精确匹配、主键重复、晚失败原子性、identity 碰撞和 256 字符边界、标准库字段长度边界。实际独立子进程摄取后退出，另一进程重开 SQLite Runtime，核对完整投影并逐条用精确 MemoryCitation 回查。

最终 **70 项本题测试在容器中用时 8.75 秒，12 项原有回归用时 3.25 秒**。容器实测 1 CPU 配额、2 GiB cgroup 内存、无 swap、512 pids、非 root、断网、只读源码；临时数据库只写临时目录。

官方固定 CentOS 镜像拉取在 360 秒预算内未完成。本次使用固定 digest 的本地 `python:3.12-slim` 替代镜像，Python 3.12.13 / Debian glibc 2.41，挂载精确匹配的固定依赖，native 扩展及真实 SQLite Runtime 均实测成功。**这是同资源限制的本地验证，不是官方镜像验证，也不是官方评测结果。**

## 优化记录

所有测量使用本机 Xeon x86_64。常规比较固定 CPU79、Python 3.12.7、单进程 `RLIMIT_AS=2 GiB`，禁止字节码写入，固定 hash seed。此地址空间上限与 cgroup 物理内存配额不同；最终另做真实 cgroup 容器测试。

主工作负载：100,000 行 × 20 列，22,666,860 bytes；包含中文、非 ASCII、空字符串、数值样式字符串、逗号、引号、CRLF、带空格主键。每次解析计时结束后逐字段核对全部行，另验证 Source projection/read。下表为各轮 7 次解析中位数；RSS 是整个 benchmark 进程峰值，包含输入生成和正确性检查。

| 轮次 | 改动 | 解析中位数 | 峰值 RSS | 决定 |
| --- | --- | ---: | ---: | --- |
| V1 | 直接完整解码，每行编码文档 ID，正常模型构造 | 1.3981 s | 386.36 MiB | 保留为有效基线 |
| V2 | 复用已编码文档前缀 | 1.2742 s | 386.25 MiB | 接受，67 项测试通过 |
| V3 | 增量 UTF-8 解码，减少整份 Unicode 副本 | 1.2004 s | 305.20 MiB | 接受，67 项测试通过 |
| V4 | 已验证字段直接构造行，复用不可变表头 | 1.1182 s | 284.15 MiB | 接受；补齐首行延迟编码边界后形成最终候选 |
| V5 | 逐行字典 `model_validate` | 1.2336 s | 304.10 MiB | 拒绝，比 V4 慢 |
| V6 | 全文收集字典，再 TypeAdapter 批量验证 | 1.0925 s | 347.36 MiB | 拒绝，额外内存换取小幅耗时收益 |

V5/V6 为性能实验，完成同一工作负载逐字段验证，未作为完整验收通过的提交候选。其源码及原始数据保存在审计包中。不会将被拒绝方案的速度列为交付方案的提升。

为降低执行次序和波动影响，又交替运行 V1、最终候选及 V6，各 3 个独立进程、每进程 3 次解析。**9 次汇总中位数：V1 1.4732 秒，最终候选 1.2138 秒，耗时降低 17.6%；峰值 RSS 385.02 → 284.43 MiB，降低 26.1%。** 这些比较在同一个宿主环境完成；不与容器数字直接计算倍数。

V6 交替复测中位数 1.1402 秒，比最终候选快约 6.1%，但峰值内存增加约 22.1%，且公开规则不对这部分速度加分，因此保留内存占用更低、完成全部验收的 V4。没有为了微小吞吐变化改动公共 Runtime。主要重复编码、整份文本副本和行模型构造方向已实测；两轮替代构造方案未提供更合适的综合结果，结束本轮优化。未证明全局最优。

最终额外覆盖：12 × 20、10,000 × 5、1,000 × 200，以及受限容器中 **300,000 × 20**。后者输入 68,666,860 bytes，3 次解析为 4.8526 / 4.6686 / 4.6561 秒，中位数 **4.6686 秒**，峰值 RSS **780.00 MiB**，1,000 行 projection/read **0.08827 秒**。每轮检查全部字段。

大规模数字只衡量解析及指定数量的 projection/read，**不代表 30 万行已全部执行 Runtime 入库与 Memory 检索**；真实持久化测试使用公开 12 行和补充 3 行。

## 运行与复现

从产品仓库根目录运行。安装阶段需要网络，以下测试与 benchmark 无需网络或模型凭据：

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-csv

PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -q \
  -c competition/pytest.ini --confcutdir=tests/sources \
  tests/sources/test_csv_rows_competition.py

PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -q \
  -c competition/pytest.ini --confcutdir=tests \
  tests/test_sources.py tests/test_source_observations.py
```

单核宿主 benchmark，选择当前允许的 CPU，不假设机器有 CPU79：

```bash
CSV_CPU=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
taskset -c "$CSV_CPU" prlimit --as=2147483648 \
  env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  competition/.venv/bin/python scripts/benchmark_csv_rows.py \
  --rows 100000 --columns 20 --repeats 7 --project 10000
```

官方容器具备下载条件时：

```bash
make -C competition docker-build
cd competition
docker compose run --rm selftest python -m pytest -q \
  -c /selftest/pytest.ini --confcutdir=/selftest \
  /selftest/challenges/source-01-table-evidence/acceptance
```

交付审计目录 `task2-evidence/` 包含官方资料快照、基线失败、各轮原始 JSON/源码、交替测量、独立 oracle、最终容器命令和输出。替代容器的复现入口为 `python3 task2-evidence/validate_container.py --product /absolute/path/to/product`；默认选择当前可用 CPU，可显式 `--cpu N`。该脚本使用的固定替代镜像须已在本机，固定依赖先按上述命令安装。

## 已知限制

没有官方隐藏评测成绩，也未验证参赛账户权限。单题分值和仓库可见性文档存在上述差异，最终以赛事平台及官方澄清为准。

完整解析返回 tuple 是题面要求，结果和去重集合的空间随输入增加；未承诺无限输入或未公开的规模上限。保留标准库默认单字段 131,072 字符限制，超限报 ValueError，没有修改进程全局 `csv.field_size_limit`。

未在官方 CentOS 基础镜像、其他 CPU、Python 小版本或隐藏 workload 上验证性能。没有执行其他赛题和全仓库全部测试；定向测试覆盖本题及所依赖的 Source/observation 行为。没有验证大规模 Runtime ingestion 的吞吐率或持久化容量。
