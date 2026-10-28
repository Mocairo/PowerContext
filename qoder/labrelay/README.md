# LabRelay

LabRelay 是一个科研项目交接与新人学习路径系统。项目将代码、论文、实验记录、个人笔记和实验室操作资料组织为可追溯证据，并结合 PowerContext 的交接与记忆能力，为接手人生成分阶段、可执行、可继续的学习路径。

正式产品与接口约束见：

`../../../docs/superpowers/specs/2026-10-27-labrelay-technical-specification.md`

## 当前阶段

本目录当前只包含首版项目骨架、配置文件、开发目录和示例资料占位。业务 API、前端页面、seekdb 索引写入、PowerContext 适配器和路径生成器尚未实现。

## 目录结构

```text
qoder/labrelay/
├── backend/          FastAPI、领域模型与核心服务
├── frontend/         React/Vite Web 界面
├── configs/          应用、检索、规划、安全和日志配置
├── examples/         可提交的脱敏示例科研资料
├── data/             本地运行数据，不提交真实资料
├── docs/             架构、运行、验证和 Qoder 开发记录
├── site/             后续 Qoder Sites 公开展示站
└── docker-compose.yml seekdb 本地服务
```

## 环境要求

- Python 3.11 或 3.12
- Node.js 20 或更高版本
- uv
- Git
- Docker
- PowerContext Server
- OpenAI 兼容的 Generation 和 Embedding API

后端与 seekdb 推荐使用 Linux 或 Windows WSL2。

## 首次配置

从示例配置创建本地环境文件：

```bash
cp .env.example .env
```

至少修改以下值：

- `SEEKDB_ROOT_PASSWORD`：本地 seekdb 测试口令；
- `GENERATION_*` 和 `EMBEDDING_*`：模型服务配置；
- `POWERCONTEXT_URL`：PowerContext Server 地址；
- `POWERCONTEXT_TOKEN`：启用认证时填写本地 Token。

`.env` 已被 Git 忽略，不得提交真实凭据。

## 启动 seekdb

```bash
docker compose --env-file .env up -d seekdb
```

seekdb 镜像首次启动可能需要较长时间完成引导。本地 SQL 服务默认映射到 `2881`。

## 安装依赖

后端依赖由 uv 管理：

```bash
uv sync --directory backend
```

前端依赖由 npm 管理：

```bash
npm --prefix frontend install
```

## 计划中的启动命令

当前源码实现尚未完成，以下命令作为本阶段的配置约定，不可在实现前直接运行：

```bash
make backend-dev
make frontend-dev
```

完成后，后端计划监听 `127.0.0.1:8010`，前端开发服务器监听 `127.0.0.1:5173`，PowerContext 默认运行在 `127.0.0.1:8000`。

## 示例资料约束

`examples/demo-project/` 只允许存放公开、合成或明确可再分发的资料。真实实验室服务器地址、账号、数据路径、密钥、未公开论文和联系人信息不得提交。

## 比赛仓库约束

本目录对应 Qoder × OceanBase 赛题的项目资产。最终仓库应基于 `2026-competition-ecosystem` 分支，并按比赛分支要求保留 `qoder` 资产目录和开发记录。
