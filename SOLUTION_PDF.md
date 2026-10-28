# 第四题：财报 PDF 全文解析与证据页检索

开发起点为 `0aec787fcd91184b0cee886299dd7388b6d454b9`，包含第二、三、五至十一题已完成成果，且包含冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52`。本题交付工作区和分支为 `powercontext-pdf` / `solution-pdf`。针对第四题提测反馈的修复与验证见 [PDF_REPAIR.md](PDF_REPAIR.md)。

## 要求、实现与验证清单

| 题面要求 | 实现位置 | 验证方式 |
| --- | --- | --- |
| 完整原始 PDF、全部物理页、空白页、全文与数字 | `src/powercontext/sources/pdf_pages.py` | 完整公开 503 页；自构造已知文本和表格 PDF；正文抽查 |
| 全文件 SHA-256、非空文档 ID、权限、损坏原子失败 | 同上 | 输入边界、空密码权限、损坏后页、修订回归 |
| 页级 CAPTURED Source、稳定修订身份、精确标准投影 | 同上 | Registry resolve/read、observation 序列化、真实 Runtime |
| 全页入库、重复幂等、重启可读、旧修订回查 | 原生 Source/Runtime/Memory 链路 | 公开测试；自构造真实数据库、跨进程重启 |
| 通用自然语言检索、scope、limit、不同页与精确 citation | `src/powercontext/sources/_pdf_search.py` | 公开 5 问逐条回查；跨公司、修订、scope 的已知证据测试 |
| 词频多重集合指标与 evidence-page Recall@20 | 产品自有测试与验证记录 | 自构造回归及授权离线公开标注验算；指标和限制见 PDF_REPAIR.md |
| 现有题目兼容、基线与完整 Git 交付 | 本分支 | `check-baseline`、`test-pdf`、最终 `make -C competition test` |

产品运行时仅访问输入 PDF、文档 ID、问题和 scoped Memory 的公开 API，不读取测试资产、答案或标注。已读取[完整语雀题面](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/ohy6lqb9wshgmrar)及仓库题面、acceptance 和公开 cases。网页明确公开与留出两组分别满足全部质量门槛；与仓库接口契约无冲突。

## 实现

`pdf_pages.py` 提供 `PDFDocumentInput`、`PDFPageInput`、`parse_pdf_document`、`PDF_PAGE_SOURCE_DEFINITION` 和重导出的 `search_pdf_pages`。解析使用比赛固定的 pypdf 6.19.0，直接读取完整输入字节；不做 OCR、不生成金融答案。完整原字节 SHA-256 使用小写十六进制。非空白文档 ID 原样保存，包括合法周边空白。

解析按全部物理页顺序提取，正常空白页返回空文本。先检查文件完整性、页树、Flate 压缩完整性、字体与 Form 资源，再完成所有页面提取；pypdf 可能静默恢复的错误也会检查，任何失败统一抛出 `ValueError`，无部分返回。支持空用户密码权限、null/继承资源、间接页树对象、旋转文本、Unicode 映射、图形状态字体、Form 文字和无文本扫描页。图形状态与 Form 继承字体只在 reader 内规整为 pypdf 可正确解码的操作，不改变原始输入字节。非空密码文档明确失败。

页级 Source 为原生 `CAPTURED`，完整保存文档 ID、文件摘要、物理页码与正文。Source name 是 `pdf:` 加三元组紧凑 JSON 的 SHA-256，共 68 个 ASCII 字符；编码边界明确，长 ID 不会截断。PDF 任意字节修订产生不同身份。标准投影严格为 `DOCUMENT_ID\nPAGE_TEXT`，metadata 仅含题面要求的三个键。注册、observation、Runtime ingestion、固定文本 pipeline 和 Memory 均使用原生链路。

`_pdf_search.py` 只调用传入 scoped Memory 的 `search(mode="fts")` 和 `get`。以自然语言词项、短查询、通用财务近义词和指标计算要素扩展召回候选，使用词频、逆文档频率、长度、标题短语重排；扩展短语去重，并优先匹配问题中明确提及的文档标识。通过真实 `memory.get` 核验引用后，保留单个 `pdf-page` 来源的 active 条目，按页级 Source 身份去重。公司、年份、问题 ID、页码均无固定答案表；不依赖解析进程状态、不读取数据库私有属性、不调用在线模型。

普通 Memory 原本限制单条 8192 UTF-8 字节，密集 PDF 页会因此入库失败。`canonical.py` 和 `service.py` 仅对全部来源为 `pdf-page` 的正文使用现有 Source observation 的 4 MiB 上界，统一正文规范化、内容哈希及 gate 证据读取；普通、混合及无来源 Memory 仍保留 8192 字节限制。PDF-only 正文保持原始 Unicode 码点，哈希绑定精确正文；普通 Memory 的 NFC 规范化行为和已有哈希保持不变。整份 observation 仍受 4 MiB 原生预算约束，payload 与 projection 重复保存文本也计入预算；超预算明确失败，绝不截断。

## 完整公开报告验证

公开文件 `PEPSICO_2022_10K.pdf` 的实际 SHA-256 为 `7d8a9f3374e429c58f101319e2da8a0e601a89859e1e4f6854163dd5f8ce6b47`。

- 全部 **503** 个物理页提取、入库并回读，原始问题 **5** 个；提取总词项数 **191777**，最大单页正文 **4934** UTF-8 字节，本文件无空文本页。
- 独立验证将每页提交两次，Source 和 Memory 均保持 503 条。关闭后启动独立 Python 子进程，不保留解析对象，逐页恢复 Source、投影和原始页输入，逐条 `memory.get` 回读全部 503 条正文；每个问题返回 20 个不同页引用，共 100 条，均准确回查文档、摘要、物理页码与版本。
- 人工将实际 PDF 渲染图与提取文本对照抽查物理页 19、62、64、503：最长正文、利润表、现金流量表、最后一页签署内容均保留。物理页 62 的印刷页码为 60，物理页 64 的印刷页码为 62；引用使用物理页码。利润表数字 `86,392 / 79,474 / 70,372`、`11,512 / 11,162 / 10,080` 和现金流表数字 `2,763 / 2,710 / 2,548`、`(5,207) / (4,625) / (4,240)` 保留在原页。渲染工具仅用于人工验证，不是产品依赖。
- 空白、扫描、后页损坏、权限、修订由自构造 PDF 补充覆盖。真实入库测试另覆盖相同页码的不同文档/修订、跨 scope 隔离、空 scope、零/负 limit、修订前已签发 citation 在新修订入库后仍可回查。

复现完整报告重复提交和独立进程验证（输出目录必须尚无数据库）：

```bash
PYTHONPATH=src:. competition/.venv/bin/python -m tests.validate_pdf_public \
  competition/challenges/source-03-pdf-retrieval/workload/public /tmp/pdf-validation
```

输出 `summary.json`、`restart.json`、用于验证器比较的 `expected.json` 及真实 `public.db`。检索实现仅访问 Memory，不读取这些测试输出。

## 质量测量范围

自构造质量集为 4 份完整 PDF、128 页，分为 2 个独立数据库 scope，每组 64 页、6 个自然语言问题；包含公司/年份变化、不同页面布局及财务干扰页。12 个问题涉及存货、税、债务、研发、养老金、租赁，共 20 个不同“问题—文档页”标注对，另有 40 条已知证据片段。

| 自构造指标 | 实测结果 |
| --- | --- |
| 128 页词项数 / 已知原文词项数 | 每页 1.0 |
| 40 条证据词项召回率 | 最低 1.0 |
| 128 页全文词项 F1 | 最低 1.0 |
| industrial 组 evidence-page Recall@20 | 10 / 10 = 1.0 |
| manufacturing 组 evidence-page Recall@20 | 10 / 10 = 1.0 |

文本指标使用小写 `[a-z]+|\d+` 和 `Counter` 词频多重集合交集。页召回按每问题的不同 `(document_id, physical_page)` 求交后累计分子/分母，多页问题与同页多次标注不混淆。另一个 36 页修订/scope 测试命中 4/4 个已知证据页。

以上表格为**自构造回归指标，不是 FinanceBench 官方分数**。用户另授权使用公开 FinanceBench 标注作独立离线验证：20 份原件共 3523 页、62 个公开问题，具体数据分组、真实检索指标、两个独立小组漏检及 BLOCK 标注 NBSP 差异均见 [PDF_REPAIR.md](PDF_REPAIR.md)。产品运行时不读取标注。正式 1151 页的冻结词项数与平台分组仍未知，不能声称通过隐藏验收。

候选池采用有界多查询召回，存在词汇差异、无显式关键词及大量非 PDF 条目挤占候选的相关性风险。测试说明的是实际执行范围；严格解析没有穷举所有合法 PDF 编码/字体组合。源码和输入运行时均不需要联网。

## 验证命令与兼容性

固定依赖在独立 `competition/.venv` 安装，Linux x86_64、Python 3.12，未修改官方 tests、样例、依赖、评分或既有题目实现。复现：

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-pdf
PYTHONPATH=src:. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 competition/.venv/bin/python -m pytest \
  -q -s -p pytest_asyncio.plugin -o asyncio_mode=auto \
  tests/sources/test_pdf_pages.py tests/sources/test_pdf_search.py \
  tests/sources/test_pdf_structure.py tests/sources/test_pdf_legal_resources.py \
  tests/e2e/test_pdf_ingestion.py tests/e2e/test_pdf_dense_page.py \
  tests/e2e/test_pdf_retrieval_quality.py tests/e2e/test_pdf_unicode_evidence.py \
  tests/e2e/test_pdf_financial_metrics.py
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy \
  make -C competition test
```

全题测试需要清除**该测试进程**的代理环境变量：宿主默认 SOCKS 代理使固定依赖里的 HTTP 客户端因未安装 `socksio` 而失败；未为此修改依赖或产品。系统代理配置保持原样。

本分支保留第二题 CSV、第三题 Pi、第五题重排、第六题增量上下文、第七题数量触发、第八题 embedding、第九题生命周期、第十题证据选择、第十一题混合检索成果。最终具体计数、命令退出码、完整 commit、推送状态与提测字段记录在配套 `task4-second-delivery/README.md` 和 `verification.json` 中。

最终 `make -C competition test` 退出 0，全部 **95 passed、0 failed、0 skipped**：CSV 17、Pi 15、PDF 6、重排 10、增量 14、数量触发 17、embedding 8、生命周期 4、证据选择 3、混合检索 1。`check-baseline` 退出 0。

PDF 自有测试已扩展至 101 项，包含压缩流截断、简称、完整标题和客户收入来源回归。此前普通 Source/Memory 行为保持，最终执行计数与日志见配套交付记录。公开五题及额外完整财报均经过真实持久化与逐条引用回查；公开标注验证结果和未达标项见 PDF_REPAIR.md。

变更文件使用比赛固定依赖进行 Ruff、格式与定向 ty 检查。全量 precommit 在临时克隆中执行；已知官方文件格式和全仓库开发依赖问题单独记录，没有将自动格式化修改带回产品，也未修改官方依赖来规避检查。
