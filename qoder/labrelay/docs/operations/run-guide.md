# LabRelay 运行指南

本文只写**已经在本地实际跑通**的命令。每条命令后面标注了它的验证状态，未验证的部分会明确说明，
不要照着「理论上应该可以」的步骤交付。

## 0. 两种运行模式

| 模式 | 依赖 | 用途 | 验证状态 |
| --- | --- | --- | --- |
| 离线演示 | 无 seekdb / 无 PowerContext / 无模型 Key | 走完上传→检索→交接→路径→进度→问答的完整闭环 | 已实跑（后端 486 项测试 + HTTP 冒烟 + 浏览器全流程 + 展示站实跑面板调用链） |
| 真实栈 | seekdb + PowerContext + OpenAI 兼容模型 | 生产形态 | seekdb 已实跑；PowerContext 服务端**从未实跑**，见 `powercontext.md` |

离线演示不是「假数据糊弄过去」：仓储、检索、规划校验、进度状态机、交接链路、凭据脱敏全部走的是
同一份生产代码，只有四个端口（repository / search engine / context gateway / model gateway）换成
确定性实现。路径生成阶段仍会检查必需证据类型，缺证据照样只出草稿。

## 1. 环境要求

- Python 3.11（`requires-python = ">=3.11,<3.13"`）
- Node.js 20+，npm
- [uv](https://docs.astral.sh/uv/)
- Git Bash（Windows 上 `make` 的 shell 循环需要它）
- Docker（只在真实栈需要；Windows 上实测走 WSL2）

## 2. 安装

```bash
cd qoder/labrelay
cp .env.example .env                  # 全部占位值，不要提交 .env
uv sync --directory backend --extra dev
npm --prefix frontend install
```

后端依赖必须带 `--extra dev`，否则 pytest 和 ruff 不在虚拟环境里。

> **关于 `Makefile`**：仓库提供 `make backend-dev` 一类的简写，但它需要 GNU make。
> Git Bash **不自带 make**，Windows 上实测 `make: command not found`，WSL 里也不一定有。
> 本文下面一律给出**实际跑过的原始命令**，make 目标只作为等价简写注在后面。

| make 目标 | 等价原始命令 |
| --- | --- |
| `make backend-install` | `uv sync --directory backend --extra dev` |
| `make backend-dev` | `PYTHONPATH="$(pwd)/backend/src" LABRELAY_USE_FAKE_DEPS=1 uv run --directory backend uvicorn labrelay.api.main:create_default_app --factory --reload --host 127.0.0.1 --port 8010` |
| `make backend-test` | `uv run --directory backend pytest` |
| `make frontend-dev` | `npm --prefix frontend run dev` |
| `make frontend-build` | `npm --prefix frontend run build` |
| `make seekdb-up` | `docker compose --env-file .env up -d seekdb` |
| `make config-check` | 逐个 `test -f` 校验配置与清单文件存在 |

## 3. 离线演示（推荐评委先跑这条路径）

三个终端：

```bash
# 终端 1：后端，端口 8010（被占用时改 LABRELAY_API_PORT）
PYTHONPATH="$(pwd)/backend/src" LABRELAY_USE_FAKE_DEPS=1 \
  uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010

# 终端 2：前端，端口 5173，/api 反代到 8010
npm --prefix frontend run dev

# 终端 3：一键端到端冒烟
uv run --directory backend python ../scripts/smoke_api.py http://127.0.0.1:8010
```

坑：`PYTHONPATH` 必须是**绝对路径**。`uv run --directory backend` 会把子进程的工作目录切到
`backend/`，写 `PYTHONPATH=backend/src` 会直接 `ModuleNotFoundError: labrelay`。
Makefile 里已用 `$(CURDIR)/backend/src` 修好这一条。

前端导入框默认值是 `../examples/demo-project`，它是**相对后端进程工作目录**解析的，
也就是 `qoder/labrelay/backend/`，因此恰好指向 `qoder/labrelay/examples/demo-project`。
从别的目录起后端就会导入失败，改用绝对路径即可。

`scripts/smoke_api.py` 自己把默认路径算成绝对路径（相对脚本位置），不受工作目录影响。

### 3.1 上传自己的资料并提问

工作台第 2 节是上传面板，走浏览器上传，不依赖服务器目录：

- 支持 Markdown、文本、PDF、代码、配置、CSV 等（与 `configs/security.yaml` 的 `allowed_text_extensions` 一致）；
- 单文件上限 **10 MB**（`configs/security.yaml` 的 `max_file_bytes`，导入器与上传接口共用）；
- 一次最多 **20 个文件**；
- 暂存目录是 `data/uploads/<project_id>/`（由 `LABRELAY_DATA_ROOT` 决定），同名文件重新上传会覆盖并更新同一个 asset，不会产生重复；
- 检测到密钥或密码的文件会保存为 `restricted`，界面显示「受限 · 未索引」，不会被检索，也不进入问答证据；
- 不支持的格式、空文件、二进制文件和超限文件会逐个返回原因，其余文件照常导入；
- 原来的「服务器本地路径导入」保留在「高级」折叠区，导入仓库自带示例资料时仍然可用。

上传完成后可直接在「3. 检索证据」核对片段，在「4. 围绕已导入资料提问」提问。问答只使用本项目检索到的资料：资料不足时回答「无法从项目资料确认」；离线确定性模型只摘录原文并注明“未做推理”，接入真实模型后才是总结性回答。

> 上传接口与其他业务接口一样没有登录认证。后端只监听 `127.0.0.1`，**不要**用 `--host 0.0.0.0` 暴露到局域网或公网。

### 3.2 接上 PowerContext（真实项目记忆）

PowerContext 1.4.1 自带一个 **无模型** 的 SQLite 模板：不需要 seekdb、不需要生成/嵌入模型，也能提供 Scope、Memory 和 PreparedContext。本机实测可用。

```bash
# 只在第一次执行：生成配置（SQLite、127.0.0.1:8000、无推理提供方）
cd qoder/labrelay
uv sync --directory backend --extra dev --extra powercontext-server
uv run --directory backend powercontext config init --template \
  --output ../data/powercontext/.env --language en

# 启动 PowerContext（前台，保持这个终端）
uv run --directory backend powercontext server run --env-file ../data/powercontext/.env
```

再按下面的组合启动 LabRelay 后端，让检索用内存索引、上下文用真实 PowerContext：

```bash
PYTHONPATH="$(pwd)/backend/src" \
LABRELAY_USE_FAKE_DEPS=1 \
LABRELAY_CONTEXT=powercontext \
LABRELAY_MODEL_PROVIDER=deterministic \
uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010
```

三个开关相互独立：

| 变量 | 取值 | 说明 |
| --- | --- | --- |
| `LABRELAY_REPOSITORY` | `memory` / `seekdb` | 项目、资料、计划存哪里 |
| `LABRELAY_CONTEXT` | `memory` / `powercontext` | 交接与问答上下文 |
| `LABRELAY_MODEL_PROVIDER` | `deterministic` / `openai_compat` / `qoder` | 生成与嵌入模型 |

问答面板会显示每次提问「自动检索了几段」「是否注入 PowerContext 上下文」「当前模型」。PowerContext 无模型模式只做词面匹配：项目创建、资料导入、交接和进度会写入项目记忆，问题与这些记忆相关时才会注入；没有匹配时显示“未使用上下文”，这是正常结果，不是接口失败。

### 3.3 使用 Qoder 内置 Qwen3.8-Flash

```bash
# 在普通终端登录一次；不要把 PAT 贴到聊天或提交到仓库
qodercli login
```

然后用 `LABRELAY_MODEL_PROVIDER=qoder` 重启后端（默认模型 `qwen3.8-flash`）：

```bash
PYTHONPATH="$(pwd)/backend/src" \
LABRELAY_CONTEXT=powercontext \
LABRELAY_MODEL_PROVIDER=qoder \
uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010
```

模型凭据优先级：`QODER_PERSONAL_ACCESS_TOKEN`（你自己的终端里设置）→ 本机 `qodercli` 登录态。未登录时问答返回 `503 dependency_unavailable`，details 里是 `dependency=model`，页面会提示登录，不会退回编造答案。也可以设置 `LABRELAY_MODEL_PROVIDER=openai_compat` 使用任意 OpenAI 兼容端点。

## 4. 真实栈

```bash
# 1) seekdb（Windows 实测经 WSL2 调用：wsl.exe docker compose ...）
docker compose --env-file .env up -d seekdb     # 首次引导可能要几分钟
docker compose --env-file .env logs -f seekdb

# 2) 后端（不带 LABRELAY_USE_FAKE_DEPS）
PYTHONPATH="$(pwd)/backend/src" \
  uv run --directory backend uvicorn labrelay.api.main:create_default_app \
  --factory --host 127.0.0.1 --port 8010

# 3) 前端
npm --prefix frontend run dev
```

真实栈还需要 `.env` 里的模型端点：`GENERATION_BASE_URL`（或 `EMBEDDING_BASE_URL` 兜底）缺失时
后端启动即报配置错误，这是刻意的——不能悄悄退回确定性假网关。

后端启动时 `create_default_app()` 会读 `.env`、加载 `configs/*.yaml`，并在 startup 里执行
`repository.initialize()`（建库建表 + 缺失列补齐）和 `context.health_check()`。
seekdb 的补列 ALTER 是一次表重写，本地实测约 18 秒，所以建表阶段单独给了 120 秒超时。

`/health/ready` 是判断依赖是否真的可用的入口，任一依赖不可用返回 503。

## 5. 端口与代理约定

| 组件 | 默认端口 | 覆盖方式 |
| --- | --- | --- |
| LabRelay 后端 | 8010 | uvicorn 的 `--port`（见下方说明） |
| LabRelay 前端 | 5173 | `LABRELAY_WEB_PORT` |
| 前端 `/api` 反代目标 | `http://127.0.0.1:8010` | `LABRELAY_API_ORIGIN` |
| seekdb SQL | 2881 | `SEEKDB_SQL_PORT` / `SEEKDB_PORT` |
| PowerContext Server | 8000 | `POWERCONTEXT_URL` |

`LABRELAY_API_PORT` / `LABRELAY_API_BIND_ADDRESS` 只写进 `LabRelayConfig.server`，**不参与实际绑定**：
用 uvicorn 命令行起服务时监听端口由 `--port` / `--host` 决定。要改端口就改命令行。

改后端端口时记得同步 `LABRELAY_API_ORIGIN`，否则前端还是打老端口。
两个会话同时开发时端口会撞，后端起不来先看是不是 8010 已被占用
（`curl -s http://127.0.0.1:8010/health/ready` 能通就说明别人在用了）。

## 6. 测试与检查

```bash
uv run --directory backend pytest                              # 486 passed, 4 skipped
uv run --directory backend ruff check src tests
uv run --directory backend ruff format --check src tests
npm --prefix frontend run typecheck
npm --prefix frontend run build
uv run --directory backend python ../scripts/smoke_api.py      # 端到端 HTTP 冒烟
```

`skipped` 的 4 项是需要真实 seekdb 的集成测试，跑法见 `seekdb.md`；
`make backend-test` 全绿**不代表** seekdb 路径被验证过。

## 7. 展示站的实跑面板接口

`qoder/labrelay/site` 的 live 面板除了普通项目接口外，还依赖三个演示专用端点，它们由
`create_app` 一并挂载，**不需要**另起一个 app 工厂：

| 端点 | 作用 |
| --- | --- |
| `GET /api/demo/status` | 报告当前接的是哪套依赖：backend / context / model / model_provider |
| `POST /api/demo/bootstrap` | 用绝对路径导入 `examples/demo-project`，建项目并生成一份待确认的交接草案 |
| `POST /api/demo/model` | 探针：让**已配置的那个**模型网关返回 `{headline, next_step}` |

要点：

- `DEMO_PROJECT_ROOT` 由包位置解析（`api/demo.py` 往上四级），不受后端工作目录影响——
  React 工作台的相对默认路径没有这个保证。
- 只有一个模型网关实现。`/api/demo/model` 走的是 `ModelGateway` 端口：离线跑时由确定性网关
  回显请求并明确标注「没有推理」，配了 `GENERATION_*` 时由真实网关作答。
  不存在第二个 Qoder 专用网关。
- 模型通道不可用（未配置或响应畸形）返回 **503 `dependency_unavailable`**，`details.dependency = "model"`，
  不会把上游错误原文带进响应体。
- CORS 只放行四个 loopback origin：`127.0.0.1`/`localhost` 的 4173 与 5173。
  实测带 `Origin: http://127.0.0.1:4173` 预检返回该 allow-origin 头，
  带 `Origin: https://evil.example` 直接 400。没有通配符。
- 面板第 3 步要用**中文查询**命中 `notes/debugging.md`，靠的是内存引擎的 CJK 单字 + 二元组切分
  （与 seekdb 的 `ngram` 全文分析器对齐）。这条由 `tests/unit/test_search.py` 钉住。

## 8. 公开展示站的边界

Qoder Sites 发布出来的是**公开 HTTPS 静态站**，浏览器在那里访问不到你本机的 `127.0.0.1:8010`，
所以「点一下就能看到自己的资料被索引」这种实跑演示只能在本地站点做。展示站只放脱敏截图、
流程说明和验证样例，实跑走本文第 3 节。
