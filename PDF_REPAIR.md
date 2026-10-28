# 第四题修复与验证

平台上已提测提交为 `b60d685ba37816ffa20472d4b385db0fdac60ca7`，用户反馈第四题 0 分，其他题均满分。仓库和提交号已核对一致。结果面板只提供各题得分，尚未取得失败断言、详细错误或正式质量标注，因此不能将某个本地缺陷认定为官方 0 分的唯一原因。

## 已复现并修复的问题

| 问题 | 复现和修复 |
| --- | --- |
| 财务指标问题漏掉计算要素所在报表 | 240 页、两家公司、16 个自构造自然语言问题中，提交版命中 5/22 个已知证据页，修复版命中 22/22。查询补充指标所需的通用报表词项，覆盖毛利率、营运资本、资产/权益回报、周转率、现金转换周期等；扩展后的同义词短语去重，避免 `inventory/inventories` 重复加分。 |
| 同类财报互相挤占候选 | 从实际文档 ID 推导公司名称，与问题中的完整词项组合进行标点无关匹配；匹配公司的候选优先，未匹配的文档继续参与排序。无公司表、固定年份、问题 ID 或页码规则。补充利润表、现金流量表标题的常见词序。 |
| Source 与 Memory 的 Unicode 原文不一致 | 原始 PDF 的 ToUnicode 可以输出分解重音字符；原实现入库时转为 NFC，违反精确正文回查。仅 PDF-only 来源保留原始码点，内容哈希绑定同一精确正文；普通/混合来源仍遵守原有 NFC 规则和大小上限。 |
| 合法 PDF 的资源/字体被误拒或误解码 | 支持 null 资源字典；`gs` 指定的字体转为 pypdf 可正确处理的文本字体状态，应用 ToUnicode 映射；Form 按各次调用保留继承字体。只变换 reader 内存对象，不改变输入 PDF 字节、SHA-256 或页码。损坏输入仍原子失败。 |

Unicode 旧数据兼容已验证：用提交版实际写入数据库，再以修复版回读旧 citation、验证原内容哈希、重建搜索索引并检索，均成功。旧版已经转换成 NFC 的 Memory 正文不会自动恢复丢失码点，也不会重写旧引用；新入库才保证与 Source 的精确码点一致。

## 验证范围

- 原公开 PepsiCo 完整 503 页和 5 个问题，继续通过原生 Source/Runtime/Memory 验证。
- 补充 3M、Apple、Microsoft、Coca-Cola 四份原始 2022 财报，共 631 页；连同 PepsiCo 共 1134 页，全部重复入库两次后去重正确，独立 Python 进程逐页回读 Source 和 Memory，21 个问题的引用逐条回查。补充问题为自写问题，不是正式 24 问题集合。
- 最终解析器输出的全部 1134 个 PDFPageInput 与此前结果逐字相等，保证字体兼容修复没有改动这些原财报的文本、摘要或物理页关系。
- 四份补充财报以 Poppler 作独立文本对照：631 页最低词频 F1 为 0.981132，词项数比例均在 0.90～1.10 内，无空白页不一致。按小写 `[a-z]+|\d+` 和词频多重集合计算；Poppler 仅用于诊断，既不是产品依赖，也不是正式 FinanceBench 标注。
- 对 16 个自写问题，另人工核对四份真实财报的利润表、资产负债表和现金流量表位置；最终命中 20/20 个主报表页。这是原始输入上的人工主报表检查，不是官方标注。
- 新增测试验证图形状态字体、ToUnicode、Form 复用、损坏原子失败、Unicode 精确正文、历史哈希兼容、财务指标与跨公司检索。测试与产品都没有读取 FinanceBench 答案或证据标注。

正式评测的每页冻结词数、证据词项召回、标注整页 F1、分组 Recall@20 仍须由平台验证。当前修复不宣称获得新的正式分数。

最终验证：全部公开题 95 passed、PDF 自有 68 passed、相关 Source/Memory 165 passed；均退出 0。定向 Ruff/格式/ty 均通过。固定依赖全仓库 ty 对照为 361 → 361 条，新增诊断 0；全量 precommit 仍因既有官方文件格式和开发环境依赖问题退出 1，自动格式化修改仅留在临时克隆。断网 1 CPU / 2 GiB / 非 root / 源码只读容器中 PDF 官方 6 项通过；该容器为固定 Debian Python 镜像，不是官方 CentOS 镜像。

## 复现

比赛固定环境可沿用独立工作区的 `competition/.venv`；没有新增依赖或改动官方测试、样例、评分逻辑。示例在交付分支执行：

```bash
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

原始失败复现、实际 PDF 来源与摘要、对照结果和最终验证日志保存在 `task4-zero-evidence/`。最终提交与推送状态、完整测试计数见配套 `task4-repair-delivery/README.md` 和 `verification.json`。
