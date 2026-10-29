# PowerContext 服务端实跑验证

2026-10-29 首次在**真实运行的 PowerContext Server** 上把 LabRelay 的交接链路端到端跑通。
本文补上 `docs/operations/powercontext.md` 里那条「服务端从未实跑过」的诚实缺口，
并记录实跑暴露、已修复的两个真实契约问题。所有观测值来自实际 HTTP 往返，不是推导。

## 结论

- 服务端：PowerContext **1.4.1**（PyPI `powercontext[server,cli]`，SQLite 内置存储，词法通道 jieba）。
- 客户端：LabRelay 的 `PowerContextService` + `HandoffService`，未改动地对接真实服务端。
- 结果：`tests/integration/test_powercontext_live.py` **1 passed**，覆盖
  Scope → 记忆写入/读回 → prepare → commit → continue → acknowledge → outcome 全链路。

## 服务端启动配方（WSL2 Ubuntu）

```bash
# 依赖（venv + pip 若缺：apt-get install -y python3-venv python3-pip）
python3 -m venv ~/pc-venv
~/pc-venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple 'powercontext[server,cli]'

# 启动：演示用 access 关闭（Server 默认），绑定 0.0.0.0 供 Windows 经 WSL NAT 访问
export POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true
~/pc-venv/bin/powercontext server run --host 0.0.0.0 --port 8100
```

WSL 处于 NAT 网络模式，Windows 侧用 `hostname -I` 的第一个地址访问；明文 HTTP 需客户端显式同意。

## 复现命令（Windows 侧）

```bash
cd qoder/labrelay/backend
POWERCONTEXT_URL="http://<WSL_IP>:8100" \
POWERCONTEXT_CLIENT_ALLOW_INSECURE_HTTP=true \
LABRELAY_POWERCONTEXT_TEST=1 \
uv run --extra dev pytest tests/integration/test_powercontext_live.py -q
```

## 服务端成功操作序列（本次实跑日志）

```text
create_scope            201
remember_memory         200   # 交接决策写入 project scope
prepare_context         200   # 记忆读回，命中 "Aurora"
create_source           201   # confirm：携带引用的证据 Source
create_work_contract    202
handoff_current_work    200
commit_handoff          200   # 得到精确 revision
continue_handoff        200   # get：按 EXACT revision 读回
acknowledge_handoff     200   # 三项 receiver checks 全 confirmed
create_source           201   # record_outcome：结果证据 Source
record_task_outcome     202
```

## 实跑暴露并修复的两个真实问题

### 1. `409 source_conflict`（已修，提交 `00285c9`）

服务端每个 capture 都以 `(scope, content, source_id)` 为身份存一行 Source，重复即冲突。
原 `confirm` / `record_outcome` 把「证据 Source 的 id」又当作 Work Contract、current-work
Handoff、Outcome 的 `source_id` 复用，导致同一 id 二次 capture。内存替身从不校验身份唯一性，
所以离线全绿也照不出来。修法：每个 capture 用各自独立的 `source_id`
（`handoff-contract:{project}:{uuid}` 等），证据 Source 仍单独保留以携带引用。

### 2. `422 invalid_access_request`（receiver 与 principal 绑定）

启用 bearer 认证（access enforced）后，服务端要求 **accepted 回执的 `receiver` 必须等于认证
principal id**（静态 bearer 固定为 `server-token`），即「不能替别人确认交接」。LabRelay 的
`receiver` 是域内人类标签（如 `new-student`），与该约束冲突。两种正确用法：

- 演示/单机：access 关闭（本配方），principal 为 `None`，守卫跳过，`receiver` 用域标签；
- 硬隔离部署：调用方以接手人身份认证，`receiver` 必须等于该 principal。

## 诚实边界

- 推理模型未配置，检索走词法通道；记忆/交接的**持久化与读回**已实测，向量化召回质量未测。
- 本次用 access-disabled + 明文 HTTP（WSL NAT 内网），非生产安全姿态。
- 服务端存储为内置 SQLite；换成 seekdb/OceanBase 后端未在本次覆盖。
