# seekdb 在 LabRelay 里负责什么

## 职责边界

```text
seekdb       = 资料、片段、引用元数据、学习计划、任务与任务事件（可查询的项目事实）
PowerContext = 决策、Work Contract、Handoff、Outcome 与跨会话记忆（可追溯的交接历史）
```

这条边界是设计上的硬约束，不是实现细节：**学习路径、任务状态和证据是否齐全，只由 seekdb 的行决定**。
PowerContext 不可用时，计划仍然能生成、进度仍然能读写、恢复学习状态仍然返回 200，
只是记忆文本降级成本地摘要并标 `memory_source`。反过来，PowerContext 里也不会去存项目事实的副本。

## 表结构

`backend/src/labrelay/seekdb/schema.sql`，7 张表：

| 表 | 主键 | 关键点 |
| --- | --- | --- |
| `projects` | `id` | `active_plan_id`、`handoff_ref`、`handoff_receipt_ref` |
| `assets` | `id` | `seq` 自增 + `uq_asset_seq`；`uq_asset_location (project_id, uri(191), checksum)` |
| `assessments` | `id` | `payload_json` |
| `handoff_drafts` | `id` | 草案只在这里落库，确认才进 PowerContext |
| `plans` | `id` | `uq_project_plan_version (project_id, version)` |
| `tasks` | `id` | `position` 决定计划内顺序，进度写入不改顺序 |
| `task_events` | `id` | 追加型；`seq` 自增保证事件顺序 |

两处 `seq` 是为「同一时间戳也要保持写入顺序」而存在的：`created_at` 精度不够，
用 id 排序又会打乱资料导入顺序。检索和进度都依赖这个顺序。

## 向量与全文混合检索

`backend/src/labrelay/retrieval/seekdb.py` 用 `pyseekdb` 的 collection：HNSW 向量索引 + 全文索引，
RRF 融合。三条刻意为之的实现约束：

1. **每个操作单独开 client、用完关掉**。共享一个连接会让上一次的 project/kind 过滤条件漏到下一次查询里。
2. **先 embed、再 upsert、最后只删过期 id**。反过来（先删后写）中途失败就把已有证据删没了。
3. **排序分来自名次，不来自距离**。不同索引实现的距离语义不一样，用名次更稳。

客户端还会再按 project / active / kind 过滤一遍——即便远端过滤条件出错，也不会把别人的资料端出去。

## 集成测试：怎么连真实 seekdb

```bash
# Windows 实测经 WSL2 调用 docker
wsl.exe docker compose --env-file .env up -d seekdb

SEEKDB_ROOT_PASSWORD=<你的本地口令> LABRELAY_SEEKDB_TEST=1 \
  uv run --directory backend pytest tests/integration -q
```

不带 `LABRELAY_SEEKDB_TEST` 时这 4 个测试 skip，`uv run --directory backend pytest` 全绿并不代表
seekdb 路径被验证过。默认打在 `labrelay_test` 库上，可用 `LABRELAY_SEEKDB_TEST_DATABASE` 改。

### 当前验证状态（2026-10-29）

本地 seekdb 容器实跑：

```text
tests/integration/test_seekdb_engine.py       1 passed
tests/integration/test_seekdb_repository.py   3 passed
4 passed in 129.16s
```

慢的部分是真实的 `ALTER TABLE ... ADD COLUMN seq BIGINT AUTO_INCREMENT`：这是一次表重写，
单条约 18 秒。也因此暴露了两个离线假连接永远测不出来的缺陷，见下一节。

## 建库与补列（initialize）

`SeekdbRepository.initialize()` 每次启动都会跑，做三件事：

1. `CREATE DATABASE IF NOT EXISTS`
2. 按 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`
3. 查 `information_schema.columns`，**只补缺失的列**：
   `projects.handoff_ref`、`projects.handoff_receipt_ref`、`assets.seq`、`task_events.seq`

第 3 步必须条件化。曾经写成「无条件执行四条 ALTER，靠吞 1060 duplicate column 兜底」，
在真实 seekdb 上直接把每次正常启动都打挂：对已经有自增列的表再加自增列，报的是
`1075 Incorrect table definition`，不是 1060。同时 10 秒的普通查询超时也扛不住 18 秒的表重写，
所以建库阶段单独用 `DDL_TIMEOUT_SECONDS = 120`。

## 公开展示的脱敏约束

真实实验数据不进仓库。仓库里只有 `examples/demo-project/` 这套合成资料；
`restricted` 状态的资料只保存不索引，检索、引用与学习路径都读不到它的正文。
