# 固定的 seekdb-js 数据层合同

使用 npm `seekdb@1.4.0`，发布元数据 gitHead `65fb0cb350507622a547ba4a3786f41c6f82e027`，锁文件含 npm integrity。以下行为已经以实际安装包和本地 seekdb `5.7.25-OceanBase seekdb-v1.4.0.0` 的集成测试核对，不能仅凭 README 示例认为通过。

- `SeekdbAdminClient.createDatabase(name)` 创建独立数据库，随后关闭 admin 连接；`SeekdbClient` 使用同一专属数据库。
- `createCollection({name, schema})` 显式配置 `FulltextIndexConfig('ik', {ik_mode:'smart'})` 和 `VectorIndexConfig({hnsw:{dimension, distance:'cosine'},embeddingFunction:null})`。`getCollection({name,embeddingFunction:null})` 重开也必须显式禁用默认 embedding。
- `collection.upsert({ids,documents,metadatas,embeddings})` 使用浏览器已经算好的向量。服务端没有 embedding provider；传错模型、维度、非有限值或非归一化向量会在调用 SDK 前被拒绝。
- `hybridSearch` 的 SDK 实现先设置会话变量，再执行 `SELECT DBMS_HYBRID_SEARCH.GET_SQL(...)`，再执行数据库生成的检索 SQL，直接转换结果格式。应用没有二路查询合并算法。RRF 的 `rankConstant=60`，窗口为 `max(20,K)`。
- SDK 使用单连接，会话变量设置与 GET_SQL 不能被其他混合请求穿插。因此本进程的队列串行化完整集合/写入/刷新/查询操作，含异常恢复；未来多进程部署需要数据库级协调。
- `collection.refresh_index()` 会根据版本解析决定是否执行刷新，无法识别版本时可以静默跳过。本应用直接 `client.execute('CALL dbms_index_manager.refresh()')`，失败即保留待刷新状态，不虚报索引已就绪。
- upsert 与 registry.revision 的更新使用 `START TRANSACTION` / `COMMIT`；刷新在提交后执行。写入成功但刷新失败不回滚已提交内容，需用户重试刷新。新一批写入在旧版本已就绪后再次使知识库变为待刷新。

真实验证结果见 `artifacts/database-validation.json`。其中的合成向量用于验证数据库状态与会话行为，与 `data/evaluation.json` 上的浏览器真实模型推理测试相互独立。

官方实现参考：[collection.ts](https://github.com/oceanbase/seekdb-js/blob/65fb0cb350507622a547ba4a3786f41c6f82e027/packages/seekdb/src/collection.ts)、[client-base.ts](https://github.com/oceanbase/seekdb-js/blob/65fb0cb350507622a547ba4a3786f41c6f82e027/packages/seekdb/src/client-base.ts)、[connection.ts](https://github.com/oceanbase/seekdb-js/blob/65fb0cb350507622a547ba4a3786f41c6f82e027/packages/seekdb/src/connection.ts)。
