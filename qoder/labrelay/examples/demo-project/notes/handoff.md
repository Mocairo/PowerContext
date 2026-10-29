# 交接说明（学长 → 新人）

## 已验证状态

- baseline 在 v3 数据、seed 2026 上已完成，测试集 AUROC 0.912、F1 0.845，
  数字与 `experiments/metrics_baseline.md` 一致，脚本入口是 `code/train.py`。
- 评估链路可跑通：`code/evaluate.py` 用同一份 `configs/baseline.yaml` 即可复算指标。
- 数据快照 `/data/aurora/v3` 已冻结，不会再变动；v4 只是快照，没有跑过任何实验。

## 已知数据缓存问题

项目早期把中间张量写进了 `<data_root>/cache/`（即 `/data/aurora/cache/`）。
这批缓存文件是按旧的样本切分生成的，**如果缓存非空，训练会跳过重新采样，直接复用旧顺序**，
于是同一份配置只能跑到 AUROC 0.874 左右，看起来像模型退化，其实是数据顺序变了。

复现前必须执行：

1. 确认 `configs/baseline.yaml` 里 `data.require_clean_cache: true`；
2. 检查 `/data/aurora/cache/` 是否为空，非空则由本人清空，不要动 `/data/aurora/v3` 本体；
3. 再启动训练。

## 下一项未完成工作

优先级最高的是 **seed 消融**：只验证过 seed 2026，需要先跑 2027、2028 两个种子，
把 AUROC 波动区间确认下来，再决定是否值得做学习率消融。
学习率消融目前只有 1.0e-3（不稳定）这一个点，缺 1.0e-4 与 2.0e-4 两个中间点。

## 约束与禁止事项

- 不允许自动操作实验室服务器，所有资源申请与释放由人工按 `ops/lab_server_resources.md` 办理。
- 不覆盖历史配置与历史实验记录；要改就新建一份配置并登记新的实验名。
- 论文原文与原始数据集不公开分发，仓库里只保留合成节选。
