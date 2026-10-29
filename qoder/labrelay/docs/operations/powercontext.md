# PowerContext 在 LabRelay 里负责什么

## 职责边界

```text
PowerContext = 决策、Work Contract、Handoff（Prepared/Committed）、Outcome 与跨会话记忆
```

项目事实（资料、片段、引用、计划、任务、事件）一律在 seekdb，PowerContext 里不放副本。
PowerContext 承担的是「为什么这么决定、交接内容是什么、上一会话说过什么」这类需要
可追溯、可续接的历史。

## 用到的 Scope 模型

每个项目两个 Scope：

| Scope | id 形态 | 装什么 |
| --- | --- | --- |
| project scope | `{project.title}:project:{project.id}` 建出来的 scope_id | 交接决策、Work Contract、Handoff、Outcome |
| learner scope | `{project.title}:learner:{project.id}` 建出来的 scope_id | 新人进度、卡点、路径版本变化 |

Scope 通过 `CreateScopeRequest(idempotency_key="labrelay:project:{id}")` 创建，
重复创建同一个项目不会开出两套 Scope。

## 适配器覆盖的操作

`backend/src/labrelay/context/powercontext.py` 是对 `powercontext[client]>=1.4.1` 的薄封装，
应用层看不到生成模型类型。用到的客户端调用：

| LabRelay 动作 | PowerContext 调用 |
| --- | --- |
| 建 Scope | `create_scope` |
| 记一条记忆 | `remember_memory` |
| 取有界历史上下文 | `prepare_context` |
| 提交交接证据 Source | `create_source` |
| 建 Work Contract | `create_work_contract` |
| 生成当前工作交接 | `handoff_current_work` |
| 固化交接版本 | `commit_handoff` |
| 续接/读取交接 | `continue_handoff` |
| 接手人回执 | `acknowledge_handoff` |
| 记录任务 Outcome | `record_task_outcome` |
| 存活探测 | `get_liveness` |

故障映射：`TransportError` / `UnavailableResponseError` / 5xx / 401 / 403 一律转成
`PowerContextUnavailableError`，API 层回 503 `dependency_unavailable`。
其余 4xx 原样抛出——那是请求本身错了，不该谎报成服务不可用。

## 交接链路的三条硬规则

1. **prepare 只入库，不产生 PowerContext 版本。** 草案存在 seekdb 的 `handoff_drafts`，
   只有 `/handoff/confirm` 才会建 Source、Work Contract 并 commit 出一个 revision。
   这一条保证交接版本不会在用户没确认的情况下出现。
2. **accepted 回执必须三项 receiver checks 全 `confirmed`。** PowerContext 服务端会拒绝
   `live_state` / `capability` / `authorization` 未全部确认的 accepted 回执。LabRelay 在请求模型里
   先校验，客户端错误提前变成 422，而不是打成一个 500。非 accepted 回执必须带 message。
3. **Outcome 不等于经验被批准。** 记录 Outcome 只登记为 `untrusted_observation`，
   不会把任务结果升格成经确认的项目知识。

## LabRelay 引用与 PowerContext 引用不是一回事

LabRelay 的 Citation 指向 seekdb 里的 asset/chunk（`asset_id` + locator），
PowerContext 的引用是 `ArtifactReference` / `SourceReference`。
交接里两侧语义不同，实现上的处理是：Work Contract 的 facts 用 `basis = declared` + 空 evidence，
真实的 LabRelay 引用随前一步创建的 Source 正文一起带过去，不伪装成 PowerContext 的 artifact ref。

## 启动与实跑（2026-10-29 已跑通真实服务端）

后端依赖装的是 `powercontext[client]`，不含服务端 CLI（`powercontext` 命令需要 `typer`）。
实跑用的是 WSL2 Ubuntu 里独立安装的 1.4.1 服务端：

```bash
# WSL2 Ubuntu
python3 -m venv ~/pc-venv
~/pc-venv/bin/pip install 'powercontext[server,cli]'
export POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true
~/pc-venv/bin/powercontext server run --host 0.0.0.0 --port 8100
```

Windows 侧驱动门控的 live 测试：

```bash
cd qoder/labrelay/backend
POWERCONTEXT_URL="http://127.0.0.1:8100" \
POWERCONTEXT_CLIENT_ALLOW_INSECURE_HTTP=true \
LABRELAY_POWERCONTEXT_TEST=1 \
uv run --extra dev pytest tests/integration/test_powercontext_live.py -q
```

不带 `LABRELAY_POWERCONTEXT_TEST` 时该测试 skip，因此离线 CI 全绿不代表连过真实服务端——
和 seekdb 那 4 项是同一个道理。逐条观测记录见 `../verification/powercontext-live.md`。

### 实跑结果

```text
create_scope 201 · remember_memory 200 · prepare_context 200 · create_source 201
create_work_contract 202 · handoff_current_work 200 · commit_handoff 200
continue_handoff 200 · acknowledge_handoff 200 · create_source 201 · record_task_outcome 202
```

`tests/integration/test_powercontext_live.py` 1 passed（2026-10-29 在 Windows 侧独立复跑通过）。

### 实跑暴露并已修掉的契约问题

**`409 source_conflict`。** 服务端把每次 work capture 以 `(scope, source_type, source_id)` 存成一行
Source，同一身份再次 capture 且正文不同就报 `SourceConflictError`（`identity` 冲突）。
`confirm` 原先把证据 Source 的 id 又当作 Work Contract 与 current-work Handoff 的 `source_id` 复用，
所以 `create_source` 之后的**第一个 capture 就必然 409**；`record_outcome` 同理。
内存替身从不校验 Source 身份唯一性，离线全绿也照不出来。
修法是每个 capture 用自己独立的 id（`handoff-contract:{project}:{uuid}` 等），
证据 Source 仍单独创建以携带引用。已验证因果：把改动反向打回去，live 测试立刻
`HTTP 409 (source_conflict)`。

**`422 invalid_access_request`（receiver 与 principal 绑定，属部署约束不是代码缺陷）。**
服务端启用 bearer 认证（access enforced）后要求 **accepted 回执的 `receiver` 等于认证 principal id**
（静态 bearer 固定为 `server-token`），语义是「不能替别人确认交接」。LabRelay 的 `receiver` 是
域内人类标签（如 `new-student`），与该约束冲突。两种正确用法：

| 部署形态 | access | `receiver` 取值 |
| --- | --- | --- |
| 演示 / 单机 | 关闭 | 域标签自由使用（principal 为 None，守卫跳过） |
| 硬隔离 | enforced | 调用方必须以接手人身份认证，`receiver` 必须等于该 principal |

LabRelay 不在客户端强行改写 `receiver`：域标签要保留给学习和交接记录阅读。
**因此演示场景必须 access 关闭**，这一点在 `../verification/troubleshooting.md` 也有对应条目。

### 仍未验证的部分

- 未配置推理模型，检索走词法通道；记忆与交接的**持久化与读回**已实测，向量召回质量未测。
- 本次是 access 关闭 + 明文 HTTP（WSL NAT 内网），非生产安全姿态。
- 服务端用内置 SQLite 存储；换成 seekdb/OceanBase 后端未覆盖。

## 不可用时的降级

PowerContext 挂了不影响项目事实和进度：

| 场景 | 行为 |
| --- | --- |
| 检索 / 计划生成 / 任务读写 | 正常，200 |
| 学习状态恢复 `learning-context` | 正常 200，记忆文本回退本地摘要，`memory_source` 标明来源 |
| 交接确认 / 回执 / Outcome | 503 `dependency_unavailable`，不会写半个版本进 seekdb |
| `/health/ready` | 503 |
