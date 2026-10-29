# Demo Research Project / 合成示例科研资料

该目录是一个**完全合成**的微型科研项目，用来让评委和开发者在本地复现 LabRelay 的主要流程：
导入资料 → 混合检索 → 学长交接 → 新人测评 → 生成七阶段学习路径 → 推进与重规划 → 反向交接。

项目名称 Aurora 时序异常检测、论文、代码、指标、服务器规则均为虚构，不对应任何真实实验室。

## 目录结构

```text
examples/demo-project/
├── papers/     合成论文节选（PDF，可抽取正文）—— 唯一的 paper 类型证据
├── code/       train.py / evaluate.py 两个入口的最小骨架
├── configs/    baseline.yaml（产出历史指标的唯一配置）+ lr_ablation.yaml（缺实验段的起点）
├── data/       数据版本、结构与读取约定说明
├── experiments/metrics_baseline.md 历史指标叙述 + metrics.csv 逐 run 指标表
├── notes/      交接说明、复现记录、调试笔记、凭据检测样例
└── ops/        服务器与资源申请/释放规则
```

共 14 项可导入资料（其中 1 项因命中凭据检测被标为 restricted），当前切出 58 个片段——
片段数随正文长度浮动，只有 14 与 restricted=1 是稳定的。

## 七个阶段的证据覆盖

阶段必需类型是 operations / note / paper / code / config / experiment，
本目录每类都至少有一份，所以合成模型网关也能生成出**可激活**的完整七阶段路径：

| 必需类型 | 由哪些资料提供 |
| --- | --- |
| paper | `papers/aurora-benchmark-excerpt.pdf` |
| code | `code/train.py`、`code/evaluate.py` |
| config | `configs/baseline.yaml`、`configs/lr_ablation.yaml` |
| experiment | `experiments/metrics.csv`、`experiments/metrics_baseline.md` |
| operations | `ops/lab_server_resources.md` |
| note | `notes/handoff.md`、`notes/reproduction_log.md`、`notes/debugging.md`、`data/README.md`、`README.md` |

> `paper` 只由 PDF 提供：导入器只把 `.pdf` 归为 paper，`.md` 无论放在哪个目录都是 note。
> 因此把论文节选换成 Markdown 会让阶段 1 缺证据、计划停在 draft。

## 展示站实跑面板依赖的两处锚点

`qoder/labrelay/site` 的 live 面板通过 `/api/demo/bootstrap` 引用本目录里两份资料的具体位置，
改文件名或删段落会直接让面板的引用变空：

- `experiments/metrics.csv` —— 需要保留 `run_key` 列与 `baseline` 那一行；
- `notes/debugging.md` —— 需要保留 `## 数据缓存` 这一节标题。

## 六个验证问题与答案出处

导入本目录后，下面六个问题都应能检索到带引用的答案：

| 验证问题               | 答案所在资料                                              |
| ---------------------- | --------------------------------------------------------- |
| 如何复现 baseline      | `notes/reproduction_log.md`                               |
| 实验配置在哪里         | `configs/baseline.yaml`（消融起点在 `configs/lr_ablation.yaml`） |
| 历史指标是多少         | `experiments/metrics_baseline.md`、`experiments/metrics.csv` |
| 已知数据缓存问题是什么 | `notes/handoff.md`、`notes/debugging.md` 的「数据缓存」节 |
| 下一项未完成工作是什么 | `notes/handoff.md`、`metrics.csv` 里两个空 seed 行、论文结论页 |
| 实验室资源如何申请释放 | `ops/lab_server_resources.md`                             |
| 数据版本与结构         | `data/README.md`                                          |

## 内置的安全样例

`notes/credential_scan_demo.md` 里含有一条**明显不可用的占位凭据**，用来演示导入阶段的凭据检测：
该文件会被标为 restricted，只保存不索引，检索与引用都不会命中它。导入结果里的
`restricted` 计数应当包含它，这是预期行为。

## 禁止放入

- 真实实验室服务器地址、账号或内网信息；
- API Key、数据库密码、私钥或其他凭据；
- 未公开或不可再分发的论文全文；
- 原始大型数据集和模型权重；
- 姓名、联系方式等个人身份信息。
