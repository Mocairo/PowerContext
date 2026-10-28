# 第四题修复与真实质量验证

用户反馈 `b60d685ba37816ffa20472d4b385db0fdac60ca7` 和 `032a0dd369a50868a6f65f1548b3ec934c3bc106` 在平台执行成功，但第四题均为 0 分，其余题满分。没有取得平台失败断言，不能把本地缺陷认定为正式 0 分的唯一原因。本分支保留其他题目实现，未修改官方测试、样例、固定依赖或评分。

## 修复内容

| 可复现问题 | 实现与回归 |
| --- | --- |
| 某页压缩流截断后，pypdf 返回空串或正文前缀，不抛异常 | `pdf_pages.py` 按过滤器链检查 Flate 完整结束标记，覆盖页面、内容数组及 Form；合法空密码及完整过滤器链继续成功，损坏输入整体 `ValueError`。新增 24 个压缩完整性用例。 |
| 去重问题词项破坏连续公司名，简称不能匹配完整文档 ID | `_pdf_search.py` 保留原问题词序，搜索权重另行去重；通过真实正文中的公司别名声明或自身股票代码识别简称，不使用公司名映射表。 |
| 关键词准入漏掉报表页面 | 从问题、通用财务概念和简称获取候选；对实际识别出的文档 ID 再通过公开 `Memory.search` 扩展候选，每个文档有界取最多 `max(1000, limit*4)` 条，再严格按文档 ID 核对。 |
| 核心报表被讨论、审计或附注挤出前 20 | 财务问题识别页首完整标题行及相邻两行的合并报表；保留财务摘要表的词项分数，但独立主报表优先。增加原问题名词标题、相邻词和收入来源关系的相关性信号。 |
| 财务概念与报表术语不同 | 通用扩展补充资本密集、PPNE、有机/分部增长、客户集中及经济周期等概念；无问题 ID、公司、年份、页码答案表。 |

检索仍只调用传入 scoped Memory 的 `search/get`，不调用 `list`，不访问数据库私有对象，不读取标注，不依赖全局页面缓存。所有返回 citation 均来自真实搜索命中，经精确 `get` 校验后按页级 Source 去重。原有全字节摘要、物理页顺序、修订身份、CAPTURED Source 和标准投影保持不变。

## 公开标注的使用边界

用户明确授权独立离线使用 FinanceBench 公开标注进行质量验证。数据固定于 revision `cc39aeb4afdf33909ee1412188bf89035950c2eb`；`financebench_open_source.jsonl` 的 SHA-256 为 `a5a2aa673e573e55675fc3c0f9aa38c1cf59d2abc91edb077534f71f10a71877`。

标注、完整 PDF、预解析验证结果及诊断脚本位于仓库外的 `task4-second-evidence/`，不进入 `src`。检索子进程仅收到数据库、scope 和自然语言问题；另一个离线评分程序再将真实引用与公开标注比较。没有取得或使用平台隐藏资产。文本按小写 `[a-z]+|\d+` 和 `Counter` 频次多重集合计算；页召回按每题不同 `(document_id, physical_page)` 求交后累加，未使用集合文本指标或题目宏平均替代。

仓库自带独立评分工具 `tests/score_pdf_labels.py`。在 `tests.validate_pdf_public` 完成真实入库和重启检索之后执行 `PYTHONPATH=src:. competition/.venv/bin/python -m tests.score_pdf_labels LABELS.jsonl OUTPUT/expected.json OUTPUT/restart.json`。该程序不调用产品检索，只读取已经生成的引用结果进行评分；任何文档组或文本质量不足时退出 1，完整冻结词数检查明确标记为不可用。

## 实际验证结果

| 数据范围 | 页数 / 问题数 | 032 提测版 Recall@20 | 本分支 Recall@20 |
| --- | --- | --- | --- |
| 原公开 PepsiCo | 503 / 5 | 8/8 | 8/8 |
| PepsiCo、3M、Apple、Microsoft、Coca-Cola 混合库中有标注的问题 | 1134 / 9 | 12/15 | 15/15 |
| 额外 10 份完整财报；经问题诊断的扩展验证集 | 1690 / 43 | 34/52 = 65.38% | 43/52 = 82.69% |
| 额外 5 家公司；未用于检索调试的独立验证集 | 699 / 10 | 未测 | 11/13 = 84.62% |

第一行是第二行的子集，不重复计入总量。合计 20 份完整原始 PDF、3523 个物理页、62 个不同公开问题。所有页面通过真实 Registry、observation、Runtime pipeline 双次提交，幂等后关闭数据库，独立 Python 进程逐页恢复 Source 和 Memory 正文；所有检索引用逐条核对摘要、物理页、正文和版本。最终解析器重新解析前两批的 2824 页，与原入库文本逐字一致；第三批直接使用最终解析器。

扩展验证集的 10 个文档组分别为 AES 5/5、Amcor 3/4、AMD 5/7、American Express 5/7、Best Buy 3/3、Boeing 8/9、CVS Health 4/5、Johnson & Johnson 3/4、Pfizer 4/4、Verizon 3/4，均达到 0.70。该批数据已用于诊断，不能称为盲测。

独立验证集各文档组为 Activision Blizzard 4/4、Adobe 1/2、Amazon 2/3、Block 2/2、Corning 2/2。总体超过 0.70，但 Adobe、Amazon 两个小组不足；没有隐去这些失败，也未用这批问题继续调参。文档组仅用于诊断，不等同于平台的数据划分。

前两批 52 问对应的 68 条标注证据，最低证据词项召回为 0.964497，最低整页词项 F1 为 0.958140，均达到 0.95。公开 PepsiCo 五题对应指标最低为 0.996212、0.979346。第三批 13 条证据中，两条不满足词项质量门槛，限制见下节。

## 未解决的正式质量不确定性

BLOCK_2020_10K 物理页 86、90 的公开标注删除词间 NBSP，产生连词；产品与原生 pypdf 的正文完全相同，保留这些空白。两页原提取词数 338、599，标注词数 263、351，正文 F1 分别为 0.762063、0.614737。第三批最低证据词项召回为 0.831909。

仅删除 NBSP 可以让这两页 F1 升至 0.955056、0.978784，但词项数降至原提取的 80.18%、59.43%；该文档 156 页会跌破原 pypdf 词数的 90%。因此本分支没有为了匹配两条标注而删除正常词间空白。

若官方冻结词数采用原始 pypdf 计数，则满足 90% 下限时，两页 F1 理论上限仅为 0.926056、0.787879，与 0.95 要求不能同时满足。官方冻结值和处理方法未公开，以上是条件性结论，不能据此认定正式题目矛盾。

正式 5 文档、1151 页、24 问题的冻结每页词数、实际数据划分及新提交分数仍不能直接验证。本分支有更充分的真实质量证据，但不宣称已经通过隐藏验收。PepsiCo 地理与重组问题各有证据页位于第 19 名，检索仍存在排名余量有限的风险。

## 复现与交付

产品没有新增依赖。比赛环境中执行：

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

只对测试进程清除代理变量，以避免宿主 SOCKS 配置要求固定环境未安装的 `socksio`；未修改产品或依赖。PDF 自有测试为 101 项，覆盖旧修订、跨 scope、空白页、空密码、损坏原子性、长正文、Unicode 原文、简称、财务指标及客户收入来源。

最终验证：`check-baseline` 退出 0；`test-pdf` 为 6 passed；全部公开题 `make -C competition test` 为 95 passed、0 failed、0 skipped，退出 0；PDF 自有 101 passed，相关 Source/Memory 197 passed。断网、1 CPU、2 GiB、非 root、源码只读容器中的 PDF 官方 6 项通过（57.43 秒）；该固定 Debian Python 3.12 镜像不是官方 CentOS 镜像。

变更文件 Ruff、格式和定向 ty 通过。全仓库固定比赛环境 ty 为提测版 361 条、修复版 361 条，新增诊断 0；全量 precommit 在临时克隆退出 1，存在既有官方 JSON/导入格式及开发环境类型问题，未将自动修正带回产品。离线评分工具在扩展集退出 0；独立第二批因已披露解析及分组不足退出 1，该失败保留在交付证据中。

配套 `task4-second-delivery/README.md`、`verification.json` 记录最终命令退出码、完整提交 SHA、远端核对、离线验证脚本和证据文件。比赛平台仍由用户手动提测。
