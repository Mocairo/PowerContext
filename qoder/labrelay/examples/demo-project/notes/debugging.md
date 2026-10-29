# 调试笔记

这份笔记记的是复现过程中踩过的坑，与 `experiments/metrics.csv` 里的 run_key 一一对应。

## 数据缓存

共享存储上留着 `/data/aurora/cache/` 旧缓存，缓存键只有样本窗口哈希，**不含 `data_version`**，
所以切到 v3 之后仍然命中 v2 时期的划分结果。表现是验证集看起来正常，测试集 AUROC 从 0.912
掉到 0.874（对应 `metrics.csv` 里的 `baseline_stale_cache`）。

处理方式：

1. 训练前确认 `/data/aurora/cache/` 为空，非空则由本人清理，不要动 `/data/aurora/v3` 本体；
2. 保留 `configs/baseline.yaml` 里的 `data.require_clean_cache: true`，不要为了跑得快关掉它；
3. 第一次跑完用测试集 AUROC 与 `experiments/metrics_baseline.md` 的验收区间核对，
   落在 0.905–0.919 才算通过。

这是当前交接里最需要注意的问题：它看起来像模型退化，其实是数据顺序变了。

## 学习率两端都不好用

`baseline_lr_1e-4` 跑满 40 个 epoch 测试集只有 0.835，损失还在下降——是没收敛，不是容量不足，
先延长训练或提高学习率再判断。`baseline_lr_1e-3` 最后 10 个 epoch 抖动明显，测试集 0.879。
中间点（2.0e-4）目前没有任何记录，属于学习率消融缺的那一段。

## 阈值

评估阈值固定在 0.5。调到 0.6 之后 F1 掉到 0.791，召回率跟着下来；
不要为了让 F1 好看去动阈值，那是指标定义问题不是模型问题。

## 显存与时长

单卡 cuda:0，batch_size 128 稳定，256 会偶发 OOM。完整跑一次约 1.5 小时，
加上排队要按 `ops/lab_server_resources.md` 的 8 小时上限安排。

## 随机种子

只验证过 seed 2026。要给出稳健性结论至少需要 2027、2028 两个额外种子，
`metrics.csv` 里这两行是空的，就是留给接手人的第一项工作。
