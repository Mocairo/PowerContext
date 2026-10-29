# LabRelay 科研接力

LabRelay 是一个科研项目交接与新人学习路径系统。项目将代码、论文、实验记录、个人笔记和实验室操作资料组织为可追溯证据，并结合 PowerContext 的交接与记忆能力，为接手人生成分阶段、可执行、可继续的学习路径。

正式产品与接口约束见：

`docs/superpowers/specs/2026-10-27-labrelay-technical-specification.md`（仓库根目录）

## 当前状态

实施计划 17 项任务中，第 1–16 项（配置、领域模型、导入脱敏、检索与引用、仓储、REST API、seekdb
持久化与检索、模型网关、PowerContext 交接链路、测评与路径生成、进度与重规划、运行时装配、资料问答、
React 前端、示例与端到端验收、运行与验证文档）已落地；第 17 项 Qoder Sites 公开展示站尚未开始，
`site/` 目前只是占位目录。

```text
后端  493 passed, 5 skipped（skipped 是需要真实 seekdb 或真实 PowerContext 服务端的集成测试）
前端  tsc --noEmit 与 vite build 均通过
实跑  离线端到端 HTTP 冒烟全绿；浏览器完成建项目→导入→检索→交接→测评→路径→推进→问答
seekdb  4 项集成测试连本地容器通过（2026-10-29）
PowerContext  1.4.1 服务端已实跑（2026-10-29），交接全链路 2xx；实跑暴露并修掉 409 source_conflict
```

## 目录结构

```text
qoder/labrelay/
├── backend/          FastAPI、领域模型、检索、规划、进度与适配器
├── frontend/         React/Vite Web 界面
├── configs/          应用、检索、规划、安全和日志配置
├── examples/         可提交的合成示例科研资料
├── data/             本地运行数据，不提交真实资料
├── docs/             架构、运行、验证和 Qoder 开发记录
├── scripts/          端到端 HTTP 冒烟脚本
├── site/             Qoder Sites 公开展示站
└── docker-compose.yml seekdb 本地服务
```

## 文档

| 想做什么 | 看哪份 |
| --- | --- |
| 把项目跑起来 | `docs/operations/run-guide.md` |
| 了解 seekdb 负责什么、怎么连真实库 | `docs/operations/seekdb.md` |
| 了解 PowerContext 负责什么、边界在哪 | `docs/operations/powercontext.md` |
| 逐步验收 | `docs/verification/acceptance-sample.md` |
| 报错了 | `docs/verification/troubleshooting.md` |

## 三十秒跑通演示

```bash
cp .env.example .env
uv sync --directory backend --extra dev
npm --prefix frontend install

PYTHONPATH="$(pwd)/backend/src" LABRELAY_USE_FAKE_DEPS=1 \
  uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010                    # 终端 1

npm --prefix frontend run dev                                # 终端 2
uv run --directory backend python ../scripts/smoke_api.py    # 终端 3
```

不需要 Docker，不需要模型 Key，不需要 PowerContext 服务。浏览器打开 `http://127.0.0.1:5173`，
导入框保持默认的 `../examples/demo-project` 即可。

仓库另有 `Makefile` 简写（`make backend-dev` 等），但 Git Bash 不自带 GNU make，
Windows 上需要先自行安装；`docs/operations/run-guide.md` 里给了每个目标等价的原始命令。

## 数据职责划分

```text
seekdb       = 资料、片段、引用元数据、学习计划、任务与任务事件（可查询的项目事实）
PowerContext = 决策、Work Contract、Handoff、Outcome 与跨会话记忆（可追溯的交接历史）
```

PowerContext 不可用时，检索、计划、进度和跨会话恢复照常工作，只有记忆文本降级为本地摘要；
交接确认与回执返回 503，不会写出半截版本。

## 首次配置

从示例配置创建本地环境文件：

```bash
cp .env.example .env
```

真实栈至少需要修改：

- `SEEKDB_ROOT_PASSWORD`：本地 seekdb 测试口令；
- `GENERATION_*` 和 `EMBEDDING_*`：模型服务配置；
- `POWERCONTEXT_URL`：PowerContext Server 地址；
- `POWERCONTEXT_TOKEN`：启用认证时填写本地 Token，留空仅适用于本地无认证测试。

`.env` 已被 Git 忽略，示例文件只含占位值，不得提交真实凭据。

## 端口约定

后端默认 `127.0.0.1:8010`（`LABRELAY_API_PORT`），前端默认 `127.0.0.1:5173`（`LABRELAY_WEB_PORT`），
前端 `/api` 反代目标由 `LABRELAY_API_ORIGIN` 决定；seekdb SQL 端口 `2881`；
PowerContext 默认 `127.0.0.1:8000`。改后端端口时三处要一起改。

## 示例资料约束

`examples/demo-project/` 是一套**完全合成**的微型科研项目，覆盖七个阶段各自需要的证据类型，
可回答验收清单里的六个验证问题。其中 `notes/credential_scan_demo.md` 刻意保留一条明显不可用的
占位凭据，用来演示导入阶段的凭据检测与受限处理。

真实实验室服务器地址、账号、内网信息、密钥、未公开论文、原始数据集和联系人信息不得提交。

## 公开展示的边界

Qoder Sites 发布的是公开 HTTPS 静态站，浏览器在那里访问不到开发机的 `127.0.0.1:8010`。
「导入自己的资料并看到结果」这类实跑演示只能在本地完成，展示站只放脱敏截图、流程说明与验证样例。

## 比赛仓库约束

本目录对应 Qoder × OceanBase 赛题的项目资产。最终仓库应基于 `2026-competition-ecosystem` 分支，
并按比赛分支要求保留 `qoder` 资产目录和开发记录。
