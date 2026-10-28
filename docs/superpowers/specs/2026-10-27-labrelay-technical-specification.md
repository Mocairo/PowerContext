# LabRelay Technical Specification

| 项目 | 内容 |
| --- | --- |
| 文档状态 | Draft for Review |
| 版本 | 0.1.0 |
| 日期 | 2026-10-27 |
| 项目名称 | LabRelay（科研接力） |
| 目标赛事 | Qoder × OceanBase |
| 开发基线 | `oceanbase/powercontext` 的 `2026-competition-ecosystem` 分支 |
| 指定技术 | seekdb、PowerContext |
| 交付形态 | 本地可运行 Web 应用 + Qoder Sites 公开展示站 |

## 1. 文档目的

本文档定义 LabRelay 首版的产品边界、系统架构、核心工作流、数据模型、接口契约、安全要求、测试策略和验收标准，作为后续实现、评审复现和交付核对的依据。

LabRelay 面向科研项目交接场景。系统将代码库、论文、实验记录、个人笔记和实验室操作资料组织为可追溯证据，并结合交接人的当前工作状态与接手人的背景，生成可执行、可检查、可跨会话继续的分阶段学习路径。

## 2. 背景与问题陈述

科研项目交接通常同时涉及多种异构资料：

- 代码库及其历史版本；
- 方法论文、参考文献和阅读笔记；
- 实验配置、指标、日志和数据位置；
- 个人调试经验与未整理记录；
- 服务器、数据集、环境、资源申请和释放规范；
- 已完成事项、当前阻塞、遗留问题和下一步计划。

这些资料往往分散且缺少明确关系。接手人即使获得全部文件，也难以判断：

1. 应该先学什么；
2. 哪些资料与当前任务相关；
3. 哪些实验结果已经被验证；
4. 哪些决定和约束仍然有效；
5. 完成当前学习后应如何继续；
6. 换一个会话后如何恢复上次进度。

单纯提供全文搜索或聊天问答不能完整解决上述问题。LabRelay 的核心价值是将“资料证据”“项目交接”“学习路径”和“进度记忆”连接成闭环。

## 3. 产品目标

### 3.1 核心目标

| 编号 | 目标 |
| --- | --- |
| G-01 | 将论文、代码、实验、笔记和操作资料统一到可追溯的项目资料空间 |
| G-02 | 保存交接人确认的当前状态、关键决定、遗留问题和下一步 |
| G-03 | 根据项目资料和新人背景生成分阶段、带引用的学习路径 |
| G-04 | 将学习路径转化为可执行、可提交证据、可追踪状态的任务 |
| G-05 | 在新会话中恢复学习阶段、最近完成事项、困难和下一任务 |
| G-06 | 根据测评、完成情况和阻塞事件调整后续任务，同时保留历史证据 |
| G-07 | 提供评委能够安装、启动并按样例复现的完整项目 |

### 3.2 成功标准

首版在随附示例项目上必须满足：

- 学习任务引用可解析率为 100%；
- 实验复现任务同时关联代码入口、实验配置和历史实验记录；
- 学习路径不存在循环依赖；
- 新会话能够恢复当前阶段、最近完成任务和下一任务；
- 资料不足时明确输出“需要补充资料”，不得生成虚假引用；
- 评委按运行说明能够完成从资料导入到跨会话恢复的主要流程；
- seekdb 检索结果和 PowerContext 记忆/交接记录均可通过日志、响应或 Dashboard 核对。

## 4. 非目标

首版明确不包含：

- 多租户在线 SaaS；
- 复杂账号、组织和权限系统；
- 自动登录实验室服务器；
- 自动申请、占用或释放 GPU；
- 自动执行任意代码库命令；
- 大型原始实验数据的完整数据仓库；
- 完整 Git 托管和版本控制服务；
- 通用论文爬虫或自动获取付费论文；
- 实时多人协同编辑；
- 将完整内部科研资料部署到公开展示站。

## 5. 用户与核心场景

### 5.1 交接人

交接人是熟悉当前项目状态的研究生、科研人员或工程师，主要操作包括：

1. 创建项目并导入资料；
2. 标记重要论文、代码入口和实验；
3. 填写当前进展、已知问题和下一步；
4. 审核系统生成的交接摘要；
5. 确认并形成正式 Handoff。

### 5.2 接手人

接手人是需要继续该项目的研究生新人，主要操作包括：

1. 填写已有基础、学习目标和可投入时间；
2. 查看交接摘要和项目资料；
3. 获得分阶段学习路径；
4. 按任务阅读、配置、运行和复现；
5. 提交完成证据或记录阻塞；
6. 在新会话中继续学习；
7. 输出自己的阶段总结和反向交接草稿。

### 5.3 核心端到端场景

```text
交接人导入资料
→ 系统建立索引
→ 交接人确认 Handoff
→ 接手人完成背景测评
→ 系统生成学习路径
→ 接手人完成环境与代码任务
→ 接手人复现 baseline
→ 系统记录完成证据和当前困难
→ 新会话恢复进度
→ 系统调整后续任务
→ 接手人导出阶段总结
```

## 6. 范围、假设与约束

### 6.1 首版范围

- 单个本地应用实例；
- 一个示例科研项目；
- 一位交接人和一位活跃接手人；
- 支持多份学习路径版本，但每个项目同一时间只有一份活跃路径；
- 支持 PDF、Markdown、TXT、常见源代码、YAML/JSON 配置、CSV/JSON/JSONL 实验元数据；
- 原始文件保留在本地文件系统，检索库保存索引和必要文本；
- 公开展示站只使用脱敏或合成的示例资料。

### 6.2 环境约束

- 后端推荐运行于 Linux 或 Windows WSL2；
- 嵌入式 seekdb 不作为 Windows 原生运行依赖；
- seekdb 使用独立部署或容器方式；
- PowerContext 通过 HTTP/Python Client 接入；
- 完整学习路径生成需要 Generation 和 Embedding API；
- 不配置模型时仍允许本地资料管理和基础全文检索，但完整演示必须配置模型；
- 所有凭据只通过环境变量或本地密钥配置提供。

### 6.3 关键设计约束

- 大模型只能基于检索到的证据和已确认的交接上下文生成内容；
- 检索和模型上下文均视为不可信历史资料，不覆盖当前系统指令；
- 不将整篇论文、完整代码库或大段实验日志复制到 PowerContext Memory；
- 所有学习任务必须经过结构校验、依赖校验和引用校验；
- 学习路径重排不得删除已完成任务及其证据。

## 7. 术语

| 术语 | 定义 |
| --- | --- |
| Asset | 一份原始资料的逻辑记录，例如论文、代码文件、实验配置或操作文档 |
| Chunk | Asset 的可检索文本片段，保留原始定位信息 |
| Citation | 指向 Asset 内部具体位置的引用，例如页码、行号或章节 |
| Evidence | 证明任务完成或支持某个结论的资料、日志、指标或总结 |
| Handoff | 交接人确认的工作边界、当前状态、遗留事项和下一步 |
| Plan | 某一版本的学习路径，包含有序任务和依赖关系 |
| Task | 具有目标、前置条件、操作步骤、完成标准和证据要求的学习任务 |
| Project Scope | PowerContext 中保存项目级决定与交接状态的 Scope |
| Learner Scope | PowerContext 中保存特定接手人背景与学习进度的 Scope |
| PreparedContext | PowerContext 针对当前请求生成的大小受限的历史上下文 |

## 8. 总体架构

### 8.1 架构分层

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 表现层 | React Web UI | 项目管理、资料总览、交接确认、学习看板、任务执行和证据展示 |
| 应用层 | FastAPI | 请求校验、流程编排、状态管理、错误转换和权限边界 |
| 导入层 | Asset Importers | 文件发现、格式解析、切块、元数据和校验信息提取 |
| 检索层 | seekdb Retrieval Service | 全文、向量、结构化条件混合检索及引用解析 |
| 记忆层 | PowerContext Adapter | Memory、Source、Work Contract、Handoff、PreparedContext |
| 规划层 | Learning Planner | 测评处理、证据选择、任务生成、依赖排序和引用校验 |
| 学习层 | Progress Service | 任务状态、完成证据、阻塞事件、重规划和进度摘要 |
| 模型层 | Model Gateway | Generation 与 Embedding 请求、结构化输出和错误处理 |

### 8.2 技术选型

| 项目 | 选择 |
| --- | --- |
| 前端 | React、Vite、TypeScript |
| 后端 | Python、FastAPI |
| Python 包管理 | uv |
| 检索与结构化数据 | seekdb |
| 上下文与交接 | PowerContext Server、PowerContext Python Client |
| 模型接口 | OpenAI 兼容 Generation/Embedding API |
| 本地运行 | Linux/WSL2、Docker 或独立 seekdb 服务 |
| 公开作品网站 | Qoder Sites 静态站点 |

### 8.3 数据流

```text
原始资料
→ Importer
→ Asset + Chunk + Citation
→ seekdb 索引
→ 按交接事项和新人背景构造检索请求
→ 返回分组证据
→ 读取 PowerContext 项目 Handoff 与 Learner Memory
→ Learning Planner 生成候选 Plan
→ Schema、依赖和引用校验
→ 保存 Plan 与 Tasks
→ Task Events 驱动进度更新
→ PowerContext 写入阶段记忆
→ 后续会话通过 PreparedContext 恢复
```

## 9. 功能需求

### 9.1 项目与资料管理

| 编号 | 需求 |
| --- | --- |
| FR-IMP-001 | 用户能够创建项目并保存项目名称、研究领域和简介 |
| FR-IMP-002 | 用户能够导入本地目录或受支持的文件 |
| FR-IMP-003 | 系统能够记录代码库 commit、文件路径和内容校验值 |
| FR-IMP-004 | 系统能够按 PDF 页、Markdown 标题、代码文件/符号和实验记录进行切块 |
| FR-IMP-005 | 系统能够跳过二进制、缓存、依赖目录和超限文件 |
| FR-IMP-006 | 系统能够显示导入成功、跳过和失败的数量及原因 |
| FR-IMP-007 | 相同项目、位置和校验值的重复导入不得产生重复 Asset |
| FR-IMP-008 | 无法提取文本的扫描 PDF 必须产生明确警告 |

### 9.2 检索与引用

| 编号 | 需求 |
| --- | --- |
| FR-RET-001 | 系统支持全文检索、向量检索和元数据过滤 |
| FR-RET-002 | 检索结果必须包含 Asset 类型、标题、内容摘要和 Citation |
| FR-RET-003 | 用户能够按论文、代码、实验、笔记和操作资料过滤 |
| FR-RET-004 | 系统能够打开引用并定位到对应原始资料位置 |
| FR-RET-005 | 检索结果应按相关性排序，并避免单一 Asset 占满全部结果 |
| FR-RET-006 | 无相关结果时返回空集合，不得构造虚假来源 |

### 9.3 交接

| 编号 | 需求 |
| --- | --- |
| FR-HOF-001 | 交接人能够填写已完成事项、当前状态、已知问题和下一步 |
| FR-HOF-002 | 系统能够生成带引用的交接摘要草案 |
| FR-HOF-003 | 交接人能够编辑并确认摘要 |
| FR-HOF-004 | 确认后系统写入项目 Memory，并创建 Work Contract 和 Handoff |
| FR-HOF-005 | 只有显式提交后才形成持久 Handoff Revision |
| FR-HOF-006 | 接手人能够查看交接摘要及其证据 |
| FR-HOF-007 | 接手人能够在阶段结束后生成反向交接草稿 |

### 9.4 测评与路径生成

| 编号 | 需求 |
| --- | --- |
| FR-PLN-001 | 系统能够收集新人基础、目标和每周可投入时间 |
| FR-PLN-002 | 系统能够根据测评调整基础任务和任务粒度 |
| FR-PLN-003 | 学习路径默认覆盖规范、理论、环境、代码、实验、接手工作和总结阶段 |
| FR-PLN-004 | 每个任务必须包含目标、前置任务、步骤、预计耗时、完成标准和证据要求 |
| FR-PLN-005 | 除纯自我测评任务外，每个任务必须至少包含一条有效 Citation |
| FR-PLN-006 | 实验复现任务必须引用代码、配置和历史实验记录 |
| FR-PLN-007 | 系统必须拒绝包含循环依赖的计划 |
| FR-PLN-008 | 证据不足的任务必须标记为 `needs_material`，不得伪造内容 |
| FR-PLN-009 | 每次重新规划生成新 Plan 版本，旧版本保留只读记录 |

### 9.5 学习执行与进度

| 编号 | 需求 |
| --- | --- |
| FR-PRG-001 | 用户能够将任务标记为进行中、已完成、受阻或跳过 |
| FR-PRG-002 | 完成任务时能够提交命令摘要、日志摘要、指标或个人总结 |
| FR-PRG-003 | 系统能够展示当前阶段、总体进度、下一任务和阻塞事项 |
| FR-PRG-004 | 阶段完成后系统更新 Learner Scope 中的进度记忆 |
| FR-PRG-005 | 新会话能够恢复当前阶段、最近完成事项、困难和下一任务 |
| FR-PRG-006 | 重新规划不得修改或删除历史 Task Event |
| FR-PRG-007 | 检查点失败时，系统能够插入排障或补救任务 |
| FR-PRG-008 | 已掌握内容可以跳过，但必须保留测评或用户确认记录 |

### 9.6 任务范围内问答

| 编号 | 需求 |
| --- | --- |
| FR-QA-001 | 用户能够围绕当前任务提问 |
| FR-QA-002 | 回答必须优先使用当前任务引用及相关检索证据 |
| FR-QA-003 | 回答中的事实性资料结论必须附 Citation |
| FR-QA-004 | 检索不足时回答应明确说明无法从项目资料确认 |

## 10. 资料导入规范

### 10.1 代码库

索引范围包括：

- README 和项目文档；
- 常见源代码文件；
- 环境依赖文件；
- 训练和评估配置；
- 实验脚本；
- 小型文本日志。

默认排除：

- `.git`、`node_modules`、虚拟环境和构建目录；
- 模型权重、压缩包、媒体和二进制文件；
- 数据集原始大文件；
- 缓存和临时文件；
- 超过配置阈值的单文件。

代码 Asset 必须记录仓库标识、commit hash 和相对路径。行号引用基于导入时的内容快照；代码变化后必须重新导入，旧引用保持指向旧版本。

### 10.2 论文

- 按页提取文本，并尽量保留章节标题；
- Citation 至少包含文件名和页码；
- 解析失败不得产生空的成功记录；
- 外部论文元数据不是首版必需信息，标题无法识别时使用文件名。

### 10.3 实验资料

支持：

- YAML/JSON 配置；
- CSV/JSON/JSONL 指标；
- Markdown 实验说明；
- 文本日志摘要；
- 数据位置和模型产物位置。

实验记录尽可能关联：

- 代码 commit；
- 配置 Asset；
- 指标 Asset；
- 数据集版本或校验值；
- 随机种子；
- 运行状态；
- 交接人备注。

大型原始数据只保存 URI、说明和校验信息。

### 10.4 笔记与操作资料

- Markdown 按标题层级切块；
- TXT 按段落和大小限制切块；
- 操作资料使用 `operations` 类型；
- 资源规范、数据位置和禁止事项应可被结构化条件优先过滤；
- 检测到密码、Token、私钥等高风险内容时，导入记录标记为受限，且禁止导出到公开网站。

## 11. 数据模型与权威边界

### 11.1 seekdb 逻辑实体

#### Project

| 字段 | 说明 |
| --- | --- |
| `id` | 项目唯一标识 |
| `name` | 项目名称 |
| `research_area` | 研究领域 |
| `description` | 项目简介 |
| `powercontext_project_scope_id` | 项目 Scope ID |
| `powercontext_learner_scope_id` | 当前接手人 Scope ID |
| `active_plan_id` | 当前活跃 Plan |
| `created_at` / `updated_at` | 时间戳 |

#### Asset

| 字段 | 说明 |
| --- | --- |
| `id` | Asset 唯一标识 |
| `project_id` | 所属项目 |
| `kind` | `paper/code/experiment/note/operations/config/other` |
| `title` | 展示标题 |
| `uri` | 原始文件或仓库位置 |
| `version` | commit、数据版本或其他版本标识 |
| `checksum` | 内容校验值 |
| `metadata_json` | 类型特定元数据 |
| `status` | `active/restricted/failed` |
| `created_at` / `updated_at` | 时间戳 |

#### Chunk

| 字段 | 说明 |
| --- | --- |
| `id` | Chunk 唯一标识 |
| `asset_id` | 所属 Asset |
| `ordinal` | 原始顺序 |
| `content` | 检索正文 |
| `locator_json` | 页码、行号、章节等定位信息 |
| `token_count` | 估算或实际 Token 数 |
| `embedding` | 向量表示 |
| `content_hash` | 内容校验值 |

#### Experiment

| 字段 | 说明 |
| --- | --- |
| `id` | 实验唯一标识 |
| `project_id` | 所属项目 |
| `run_key` | 交接人或代码定义的实验标识 |
| `code_commit` | 对应代码版本 |
| `config_asset_id` | 配置 Asset |
| `metrics_asset_id` | 指标 Asset |
| `dataset_uri` | 数据位置 |
| `status` | `running/succeeded/failed/unknown` |
| `metrics_json` | 关键指标 |
| `notes` | 实验说明 |

#### Relation

用于表达“论文由代码实现”“实验评估某方法”“任务依赖某资料”等关系。每条 Relation 包含源、目标、关系类型、证据 Chunk 和状态。模型发现的关系初始为 `proposed`，交接人确认后为 `confirmed`。

#### Assessment

保存新人背景、目标、每周时间、诊断答案和系统评估结果。

#### Plan

| 字段 | 说明 |
| --- | --- |
| `id` | Plan 唯一标识 |
| `project_id` | 所属项目 |
| `version` | 单调递增版本号 |
| `status` | `draft/active/superseded` |
| `assessment_id` | 对应测评 |
| `handoff_ref` | 使用的 PowerContext Handoff 引用 |
| `context_refs_json` | 生成时使用的其他上下文引用 |
| `created_at` | 创建时间 |

#### Task

| 字段 | 说明 |
| --- | --- |
| `id` | Task 唯一标识 |
| `plan_id` | 所属 Plan |
| `stage` | 所属阶段 |
| `title` / `objective` | 标题和目标 |
| `prerequisite_task_ids` | 前置任务 |
| `estimated_minutes` | 预计耗时 |
| `citations_json` | 资料引用 |
| `steps_json` | 操作步骤 |
| `pitfalls_json` | 已知问题 |
| `completion_criteria` | 完成标准 |
| `evidence_requirements_json` | 需要提交的证据 |
| `status` | `locked/todo/in_progress/blocked/done/skipped/needs_material` |
| `position` | 阶段内顺序 |

#### TaskEvent

保存状态变化、完成证据、阻塞说明、跳过依据和重规划影响。TaskEvent 只追加，不原地覆盖。

### 11.2 PowerContext 内容

#### Project Scope

保存：

- 已确认的技术决定；
- 数据、服务器和资源使用约束；
- 当前项目状态；
- 交接人确认的 Work Contract 和 Handoff；
- 关键遗留问题和下一步；
- 必要的交接 Source 引用。

#### Learner Scope

保存：

- 新人背景和学习目标；
- 已掌握内容；
- 当前学习阶段；
- 最近完成的任务；
- 反复出现的困难；
- 当前阻塞；
- 下一任务；
- 阶段总结和任务结果 Source。

### 11.3 数据权威矩阵

| 数据 | 权威来源 |
| --- | --- |
| 原始论文、代码和实验文件 | 本地文件系统或原始仓库 |
| 检索文本、向量、结构化实验信息 | seekdb |
| Plan、Task、TaskEvent 当前状态 | seekdb |
| 项目决定、约束和 Handoff | PowerContext Project Scope |
| 新人背景摘要和跨会话学习记忆 | PowerContext Learner Scope |
| 公开展示内容 | 独立脱敏的静态站点资源 |

PowerContext 不是原始资料仓库，seekdb 也不替代 Handoff 和学习记忆。

## 12. 检索与 Citation 设计

### 12.1 检索流程

1. 根据用户问题或任务生成查询；
2. 应用项目、类型、版本和状态过滤；
3. 分别执行全文和向量检索；
4. 归一化并融合排序；
5. 对同一 Asset 的相邻 Chunk 去重或合并；
6. 限制单一 Asset 的结果占比；
7. 返回带 Citation 的证据集合。

### 12.2 Citation 结构

```json
{
  "asset_id": "asset-123",
  "kind": "code",
  "title": "src/train.py",
  "uri": "repo/src/train.py",
  "version": "commit-sha",
  "locator": {
    "line_start": 35,
    "line_end": 82
  },
  "excerpt": "用于展示的简短原文摘要"
}
```

### 12.3 引用校验

- `asset_id` 必须存在且属于当前项目；
- Asset 必须为可读取状态；
- locator 必须属于导入时保存的范围；
- 代码引用必须包含版本；
- 论文引用必须包含页码；
- 实验复现任务必须包含代码、配置和实验记录三类 Asset；
- 校验失败的 Plan 不得进入 `active` 状态。

## 13. 交接工作流

### 13.1 交接草案

交接人填写：

- 项目目标；
- 已完成事项；
- 已验证结果；
- 当前阻塞；
- 未完成事项；
- 推荐下一步；
- 不建议方向；
- 技术决定及原因；
- 环境和数据限制；
- 常见失败和排查经验。

系统根据上述字段检索相关证据，生成交接摘要草案。草案必须由交接人确认，不能自动视为事实。

### 13.2 持久化顺序

```text
保存结构化交接表单
→ 生成摘要草案
→ 交接人编辑确认
→ 写入项目 Memory
→ 创建 Work Contract
→ 准备 Handoff
→ 显式提交 Handoff Revision
→ 保存精确 Artifact 引用到 Project
```

### 13.3 接手与结果

接手人开始项目时读取当前 Handoff。阶段完成后，系统可生成任务结果 Source 和反向交接草稿。反向交接仍需用户确认，不自动覆盖交接人的原始 Handoff。

## 14. 新人测评

测评包括：

- Python、深度学习框架和 Git 基础；
- Linux 与服务器使用经验；
- 相关论文阅读情况；
- 类似实验复现经验；
- 每周可投入时间；
- 接手目标；
- 理论优先或跑通实验优先的偏好。

测评结果用于：

- 决定是否加入基础任务；
- 调整任务粒度；
- 调整理论与实验任务顺序；
- 设置初步时间预算；
- 为可跳过任务保留依据。

测评不得用于给出能力评价或排名，只用于学习计划个性化。

## 15. 学习路径生成

### 15.1 默认阶段

| 阶段 | 目标 | 必要检查点 |
| --- | --- | --- |
| 0. 项目与实验室规范 | 理解目标、数据位置和资源规则 | 能说明关键约束 |
| 1. 理论与论文基础 | 理解问题、方法和关键概念 | 能依据论文回答核心问题 |
| 2. 环境与数据准备 | 完成环境和数据检查 | 检查结果符合项目说明 |
| 3. 代码导读 | 理解入口、模块和配置关系 | 能描述主要调用链 |
| 4. 实验复现 | 复现已有 baseline | 指标与历史结果一致或差异可解释 |
| 5. 接手未完成工作 | 推进 Handoff 中的下一步 | 形成新实验或代码结果 |
| 6. 总结与反向交接 | 形成自己的项目理解 | 输出状态、证据、问题和下一步 |

阶段 5 和 6 可在同一学习周期内重复，但首版仍表示为一条线性主路径。

### 15.2 生成算法

1. 读取已确认 Handoff；
2. 读取 Learner Scope 中的背景和进度；
3. 按阶段构造检索请求；
4. 从 seekdb 选择候选证据；
5. 建立任务前置关系；
6. 请求 Generation 模型输出严格 JSON；
7. 执行 JSON Schema 校验；
8. 执行依赖图拓扑校验；
9. 执行 Citation 校验；
10. 对资料不足任务设置 `needs_material`；
11. 保存 Plan 和 Tasks；
12. 校验通过后激活 Plan。

### 15.3 Task JSON 示例

```json
{
  "stage": 4,
  "title": "复现 baseline 实验",
  "objective": "使用指定代码版本和配置复现历史指标",
  "prerequisite_task_ids": ["task-env-check", "task-code-entrypoint"],
  "estimated_minutes": 120,
  "citations": [
    {"asset_id": "asset-train", "locator": {"line_start": 1, "line_end": 80}},
    {"asset_id": "asset-config", "locator": {"section": "baseline"}},
    {"asset_id": "asset-metrics", "locator": {"run_key": "baseline"}}
  ],
  "steps": [
    "确认代码 commit 和数据版本",
    "使用 baseline 配置启动训练",
    "保存日志并对比历史指标"
  ],
  "pitfalls": ["旧数据缓存可能改变数据划分"],
  "completion_criteria": "指标处于交接记录允许范围内，或能够解释差异",
  "evidence_requirements": ["command_summary", "log_summary", "metrics_comparison"]
}
```

### 15.4 规划约束

- 不允许无前置理由地先安排实验、后安排环境和数据检查；
- 操作任务必须优先引用 `operations` 资料；
- 每个阶段的任务数量应与用户时间预算相适应；
- 预计耗时只是计划值，不作为实际提效数据；
- 无法从资料确定具体命令时，应生成资料确认任务，而不是编造命令；
- 模型输出不能直接改变 Task 状态，状态只能由用户操作或明确检查点事件改变。

## 16. 学习执行与重规划

### 16.1 状态流转

```text
locked → todo → in_progress → done
                   ↓
                 blocked → in_progress

todo/in_progress → skipped
任意未完成任务 → needs_material
```

- 只有前置任务完成、跳过或经用户确认豁免后，后续任务才能从 `locked` 转为 `todo`；
- `done` 必须关联至少一项完成证据；
- `skipped` 必须关联测评结果或用户确认；
- 状态变化写入只追加 TaskEvent。

### 16.2 重规划触发条件

- 用户修改目标或时间预算；
- 测评证明已掌握某部分内容；
- 检查点未通过；
- 资料缺失；
- 用户持续受阻并主动请求调整；
- Handoff 经交接人确认发生重要变化。

### 16.3 重规划规则

- 已完成任务和 TaskEvent 原样保留；
- 新 Plan 引用旧 Plan 作为来源；
- 旧 Plan 状态改为 `superseded`；
- 已完成任务映射到新 Plan 的对应任务或历史附录；
- 仅调整未完成和未开始任务；
- 重规划后重新执行 Schema、依赖和引用校验。

## 17. HTTP API 契约

### 17.1 主要接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/projects` | 创建项目及 PowerContext Scopes |
| `GET /api/projects/{id}` | 获取项目摘要和当前状态 |
| `POST /api/projects/{id}/imports` | 导入文件或目录 |
| `POST /api/projects/{id}/reindex` | 重建索引 |
| `GET /api/projects/{id}/assets` | 列出资料及导入状态 |
| `POST /api/projects/{id}/search` | 混合检索 |
| `POST /api/projects/{id}/handoff/draft` | 生成交接草案 |
| `POST /api/projects/{id}/handoff/confirm` | 确认并持久化 Handoff |
| `GET /api/projects/{id}/handoff` | 获取当前 Handoff |
| `POST /api/projects/{id}/assessments` | 提交新人测评 |
| `POST /api/projects/{id}/plans` | 生成学习路径 |
| `GET /api/plans/{id}` | 获取指定版本学习路径 |
| `PATCH /api/tasks/{id}` | 更新任务状态 |
| `POST /api/tasks/{id}/evidence` | 提交完成证据 |
| `POST /api/plans/{id}/replan` | 生成后续 Plan 版本 |
| `POST /api/projects/{id}/questions` | 任务范围内问答 |
| `GET /api/projects/{id}/export` | 导出阶段总结和交接草稿 |
| `GET /health/live` | 应用存活检查 |
| `GET /health/ready` | seekdb、PowerContext 和模型依赖检查 |

### 17.2 错误结构

```json
{
  "error": {
    "code": "citation_validation_failed",
    "message": "The learning plan contains an invalid citation.",
    "details": {
      "task_id": "task-123",
      "asset_id": "asset-missing"
    }
  },
  "request_id": "request-123"
}
```

### 17.3 状态码

| 状态码 | 使用场景 |
| --- | --- |
| 400 | 请求格式或参数错误 |
| 404 | Project、Plan、Task、Asset 或 Citation 不存在 |
| 409 | 当前状态不允许该操作，例如重复激活 Plan |
| 422 | 导入、模型输出、依赖或 Citation 校验失败 |
| 503 | seekdb、PowerContext 或必需模型服务不可用 |

所有错误响应必须包含 `request_id`，日志可通过该 ID 关联。

## 18. 前端页面

| 页面 | 核心内容 |
| --- | --- |
| 项目总览 | 项目目标、资料数量、当前 Handoff、学习进度和下一任务 |
| 资料中心 | Asset 列表、类型过滤、导入状态、原始位置和引用预览 |
| 检索页 | 查询、混合检索结果、类型过滤和 Citation 定位 |
| 交接页 | 结构化交接表单、摘要草案、证据和确认操作 |
| 新人测评页 | 背景、目标、时间和诊断题 |
| 学习路径页 | 阶段看板、任务依赖、状态和预计耗时 |
| 任务详情页 | 目标、步骤、资料、常见坑、完成标准和证据提交 |
| 进度页 | 当前阶段、完成记录、阻塞、下一任务和历史 Plan |
| 导出页 | 阶段总结和反向交接草稿 |

UI 必须区分“模型建议”“交接人已确认”和“系统已验证”三类信息。

## 19. 安全与隐私

### 19.1 凭据

- API Key、数据库密码和 PowerContext Token 只从环境变量读取；
- 不写入日志、Source、Memory、Plan、TaskEvent 或静态站点；
- 仓库只提交 `.env.example` 和占位值；
- 模型上下文不得包含凭据。

### 19.2 内容安全

- 检索资料和 PowerContext 历史均标记为不可信内容；
- 不执行资料或模型输出中的命令；
- 不允许模型根据资料自行扩大文件访问范围；
- 文件导入限制在用户提供的路径内；
- 默认只监听 `127.0.0.1`；
- 公开导出前执行敏感信息检查。

### 19.3 实验室资料

- 公开网站不得包含真实服务器地址、内网地址、账号、数据路径和个人联系方式；
- 演示数据必须脱敏或使用合成内容；
- 用户反馈和效果数据展示前隐去个人信息。

## 20. 异常与降级语义

| 场景 | 系统行为 |
| --- | --- |
| seekdb 不可用 | 导入、检索和 Plan 激活返回 503；UI 明确显示依赖不可用 |
| PowerContext 不可用 | 新生成 Handoff 和跨会话记忆功能返回 503；不得声称记忆已保存 |
| Embedding 不可用 | 向量检索标记不可用；可显示全文结果，但完整演示状态不视为通过 |
| Generation 不可用 | 不生成或激活新 Plan；已有本地计划和进度仍可查看 |
| 模型输出不是合法 JSON | 请求失败并记录校验错误，不直接展示为正式 Plan |
| Citation 校验失败 | Plan 保持非活跃状态，返回具体任务和 Asset |
| PDF 无法提取正文 | Asset 标记失败或受限，提示 OCR 需求 |
| 文件过大或不支持 | 跳过并记录原因，不影响其他文件导入 |
| 任务完成时记忆写入失败 | TaskEvent 保留，UI 显示“进度已保存但跨会话记忆未同步”，不得显示完整同步成功 |
| 新会话无相关 Memory | PreparedContext 返回 empty，应用明确显示无历史，不伪造进度 |

## 21. 配置与部署

### 21.1 配置项

```text
LABRELAY_STORAGE_ROOT
LABRELAY_MAX_FILE_BYTES

SEEKDB_HOST
SEEKDB_PORT
SEEKDB_USER
SEEKDB_PASSWORD
SEEKDB_DATABASE

POWERCONTEXT_URL
POWERCONTEXT_TOKEN

GENERATION_BASE_URL
GENERATION_API_KEY
GENERATION_MODEL

EMBEDDING_BASE_URL
EMBEDDING_API_KEY
EMBEDDING_MODEL
EMBEDDING_DIMENSIONS
```

实际配置名称在实现时固定于 `.env.example`，运行说明以该文件为准。

### 21.2 启动顺序

1. 启动并检查 seekdb；
2. 启动并检查 PowerContext Server；
3. 配置 Generation 和 Embedding；
4. 初始化 seekdb 表结构；
5. 启动 FastAPI；
6. 启动 React 开发服务器或构建后的静态资源服务；
7. 调用 `/health/ready`；
8. 导入示例项目并执行验收。

### 21.3 运行交付

必须提供：

- 锁定依赖；
- `.env.example`；
- seekdb 部署说明；
- PowerContext 部署说明；
- 初始化命令；
- 启动命令；
- 示例数据导入命令；
- 验证样例；
- 常见问题；
- 不提交真实凭据的说明。

## 22. 可观测性与审计

### 22.1 日志

记录：

- `request_id`；
- 操作类型；
- Project、Plan、Task 和 Asset ID；
- 导入数量与失败原因；
- 检索延迟和结果数量；
- Plan 校验结果；
- PowerContext 操作类型和精确 Artifact 引用；
- 外部依赖错误类别。

默认不记录完整论文、代码、个人笔记、模型密钥和原始敏感内容。

### 22.2 指标

- 导入成功/失败/跳过数量；
- 检索请求数和空结果率；
- Plan 生成成功率；
- Citation 校验失败率；
- 重规划次数；
- PowerContext 同步失败数；
- 任务完成和阻塞数量。

### 22.3 比赛验证记录

保留：

- seekdb 查询请求和结果；
- PowerContext Memory 写入和搜索响应；
- Handoff 精确 Revision；
- 新会话 PreparedContext 响应；
- Dashboard 截图；
- 示例项目端到端运行日志。

## 23. 测试策略

### 23.1 单元测试

- 各格式 Importer；
- Chunk 顺序和 Citation locator；
- 文件过滤与大小限制；
- 敏感信息检测；
- Plan JSON Schema；
- 依赖图拓扑排序和循环检测；
- 任务状态流转；
- Citation 校验；
- 重规划时历史任务保留。

### 23.2 集成测试

- seekdb 表初始化、写入、全文检索、向量检索和过滤；
- PowerContext Scope 创建、Memory 写入/搜索、Handoff 和 PreparedContext；
- Generation/Embedding 错误处理；
- 导入后检索；
- Plan 激活前完整校验；
- TaskEvent 与进度记忆同步。

### 23.3 端到端测试

覆盖：

1. 创建项目；
2. 导入全部示例资料；
3. 确认 Handoff；
4. 提交新人测评；
5. 生成并激活 Plan；
6. 完成环境与代码任务；
7. 提交 baseline 复现证据；
8. 记录一个阻塞；
9. 重启会话并恢复进度；
10. 重规划后续任务；
11. 导出反向交接草稿。

### 23.4 负向测试

- 扫描 PDF；
- 空代码库；
- 缺失 baseline 配置；
- 重复导入；
- 超限文件；
- 伪造 Asset ID；
- 循环任务依赖；
- 模型返回无效 JSON；
- seekdb/PowerContext/模型服务不可用；
- 资料中出现提示注入或命令文本。

## 24. 验证样例

### 24.1 初始数据

随项目提供一个微型科研示例：

- 两篇公开论文或合法可分发节选；
- 一个小型可运行代码库；
- 训练入口和评估入口；
- 一份 `baseline` 配置；
- 一份历史指标文件；
- 一份数据说明；
- 一份调试笔记；
- 一份实验室操作说明；
- 一份交接人初始交接表单。

示例交接状态包含：

- baseline 已完成并记录指标；
- 一项扩展实验未完成；
- 一个已知数据缓存问题；
- 明确的下一步；
- 资源使用和数据位置约束。

### 24.2 操作步骤

1. 启动 seekdb 和 PowerContext；
2. 启动 LabRelay；
3. 创建示例项目；
4. 导入全部资料；
5. 查询“如何复现 baseline”；
6. 确认返回代码、配置和历史指标；
7. 生成并确认 Handoff；
8. 提交“会 Python 和 PyTorch，但不熟悉实验室服务器”的新人测评；
9. 生成学习路径；
10. 检查规范、环境、代码和实验任务；
11. 完成环境与代码任务并提交证据；
12. 提交 baseline 对比结果；
13. 记录一个新问题；
14. 开始新会话；
15. 请求恢复当前学习状态；
16. 调整后续任务；
17. 导出反向交接草稿。

### 24.3 预期结果

- 检索结果包含可打开的 Citation；
- 学习路径包含规范、理论、环境、代码、实验和后续工作；
- 服务器相关任务优先于实验任务；
- 实验复现任务同时引用代码、配置和指标；
- 新会话返回当前阶段、最近完成事项、新问题和下一任务；
- seekdb 中存在实际检索记录；
- PowerContext 中存在实际 Memory、Handoff 和进度召回记录；
- 重规划不会删除已经提交的证据。

## 25. 验收标准与需求追踪

| 验收项 | 对应需求 | 验证方式 |
| --- | --- | --- |
| 多类型资料可导入 | FR-IMP-001～008 | 导入测试和示例项目 |
| 检索结果可追溯 | FR-RET-001～006 | 黄金查询和 Citation 解析测试 |
| 交接状态真实持久化 | FR-HOF-001～007 | PowerContext Handoff 与 Dashboard |
| 学习路径结构完整 | FR-PLN-001～009 | Schema、依赖和引用校验 |
| 任务可执行并记录证据 | FR-PRG-001～008 | 端到端学习流程 |
| 新会话恢复进度 | FR-PRG-004～005 | PreparedContext 跨会话测试 |
| 检索不足不编造 | FR-RET-006、FR-QA-004 | 空结果和伪造引用负向测试 |
| 评委能够复现 | G-07 | 按运行说明在干净环境执行 |
| Qoder 开发可核对 | 赛事要求 | 对话记录、截图、提交历史和 UID |
| 网站公开可访问 | 赛事要求 | 无痕浏览器访问 Qoder Sites 地址 |

## 26. Qoder 开发与比赛交付

### 26.1 仓库与复用约束

- 项目开发必须基于 `2026-competition-ecosystem` 分支，最终提交记录该分支上的 commit hash；
- 项目代码和相关资产必须按照比赛分支说明放入对应 `qoder` 目录，不自行假设目录结构；
- 复用开源代码、论文、示例数据或其他资料时，必须记录来源、许可证和所做改动；
- 示例论文和数据必须允许合法分发；不满足分发条件时仅提供获取说明，不直接打包原文件；
- 最终仓库不得包含真实 API Key、密码、私钥、实验室凭据或未脱敏个人数据。

### 26.2 开发过程记录

按阶段保留：

- 需求拆解对话；
- 架构设计对话；
- 核心功能编码对话；
- 测试生成和调试对话；
- seekdb 接入记录；
- PowerContext 接入记录；
- Qoder Sites 发布记录；
- 关键 commit hash；
- Qoder 账号 UID。

记录应展示 Qoder 实际参与主要开发，不只保留最终发布截图。

### 26.3 Qoder Sites 内容

展示站包含：

- 项目简介和真实痛点；
- 核心流程；
- 功能截图和演示视频；
- 示例学习路径；
- seekdb 与 PowerContext 分工；
- 可追溯检索和跨会话恢复证据；
- 代码仓库链接；
- 运行说明；
- 验证样例；
- 使用效果数据，如有。

展示站设置为“任何人都可访问”，并使用无痕窗口验收。真实内部资料、密钥和实验室信息不得进入站点。

### 26.4 最终提交物

- Qoder Sites 完整网址；
- 代码仓库地址；
- 完整运行说明；
- 至少一组端到端验证样例；
- Qoder 开发记录及 UID；
- seekdb 和 PowerContext 实际作用证据；
- 使用效果材料，如有。

## 27. 实施阶段

| 阶段 | 主要产物 | 完成条件 |
| --- | --- | --- |
| 0. 基线与规格 | 比赛目录、需求规格、开发记录结构 | 范围和目录规范确认 |
| 1. 端到端骨架 | 前后端、项目创建、最小导入与检索 | 最小链路可运行 |
| 2. 资料导入 | Importers、Asset、Chunk、Citation | 示例资料全部正确处理 |
| 3. seekdb 检索 | 混合检索、过滤、引用解析 | 黄金查询通过 |
| 4. PowerContext 交接 | Scopes、Memory、Handoff、PreparedContext | 跨会话恢复通过 |
| 5. 路径生成 | 测评、Planner、Schema、依赖和引用校验 | 示例 Plan 激活 |
| 6. 学习执行 | 看板、任务详情、TaskEvent、进度页 | 可完成并回看任务 |
| 7. 重规划与导出 | Plan 版本、补救任务、反向交接 | 历史证据不丢失 |
| 8. 示例与测试 | 微型科研仓库、自动化测试、验收日志 | 完整样例通过 |
| 9. 工程化 | 配置、脚本、锁定依赖、运行说明 | 干净环境可复现 |
| 10. 发布与提交 | Qoder Sites、截图、视频、提交材料 | 公开访问和材料核对通过 |

优先完成“资料导入 → 混合检索 → Handoff → 学习路径 → 完成任务 → 新会话恢复”的最短闭环，再增加界面和展示增强。

## 28. 风险与缓解

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 学习路径过于泛化 | 核心价值不足 | 强制 Citation、完成标准和证据要求 |
| 模型编造来源 | 误导接手人 | 激活 Plan 前逐条校验 Citation |
| 资料规模过大 | 索引慢、成本高 | 文件白名单、大小限制、仅索引实验元数据 |
| 双存储职责混乱 | 状态不一致 | 遵循数据权威矩阵和精确 Artifact 引用 |
| PowerContext 或 seekdb 不可用 | 核心流程失败 | 明确健康检查、错误语义和同步状态 |
| 实验室资料泄露 | 安全风险 | 本地部署、敏感检测、公开内容脱敏 |
| 代码变更导致行号失效 | 引用不可解析 | Citation 绑定 commit 和内容快照 |
| 首版范围膨胀 | 无法完成闭环 | 排除 SaaS、自动服务器操作和复杂协作 |
| Windows 环境差异 | 复现失败 | 统一使用 Linux/WSL2 后端运行说明 |

## 29. 后续扩展

以下功能不属于首版承诺，但可在核心闭环稳定后考虑：

- 多接手人独立 Learner Scope；
- 导师审核和评论；
- arXiv 元数据导入；
- 更完整的知识图谱可视化；
- 真实实验平台集成；
- 自动检查实验指标；
- 团队级权限和审计；
- 经过人工确认的安全命令执行；
- 真实用户使用效果和提效分析。

## 30. 首版最终边界

LabRelay 首版以一条可验证的科研项目交接闭环为交付目标：

```text
资料可导入
→ 证据可检索
→ 交接可持久化
→ 路径可生成
→ 任务可执行
→ 进度可恢复
→ 后续计划可调整
→ 结果可导出
```

任何不能增强该闭环或比赛验收的功能，均不属于首版优先工作。
