# 实验记录：Aurora baseline（v3 数据，seed 2026）

配置：`configs/baseline.yaml`（experiment_name `baseline_v3_seed2026`）
代码：`code/train.py` + `code/evaluate.py`（commit `aurora-demo-8f2c1a9`）

## 历史指标

| 指标   | 训练集 | 验证集 | 测试集 |
| ------ | ------ | ------ | ------ |
| AUROC  | 0.958  | 0.921  | 0.912  |
| F1     | 0.881  | 0.853  | 0.845  |
| 召回率 | 0.869  | 0.841  | 0.830  |
| 误报率 | 0.071  | 0.089  | 0.095  |

复现验收线：测试集 AUROC 落在 0.905–0.919、F1 落在 0.838–0.852 视为复现成功。
超出该区间先按 `notes/reproduction_log.md` 的排查清单检查数据缓存，再怀疑代码。

## 实验环境

- Python 3.11，单卡 cuda:0，batch_size 128，epochs 40，learning_rate 3.0e-4，seed 2026。
- 数据版本 v3（`/data/aurora/v3`），训练前已确认缓存目录为空。

## 未完成实验

1. seed 消融：只跑过 seed 2026，非默认随机种子（2027、2028）尚未验证，波动幅度未知。
2. 学习率消融：3.0e-4 之外只试过 1.0e-3，后者不稳定，缺少中间点。
3. v4 数据：v4 快照已就绪但从未跑过实验，不能作为 baseline 的对照。

## 已知失败记录

- 未清理缓存时测试集 AUROC 掉到 0.874，原因是采样顺序变化，不是模型问题。
- 把 threshold 从 0.5 调到 0.6 后 F1 降到 0.791，召回率随之下降。
