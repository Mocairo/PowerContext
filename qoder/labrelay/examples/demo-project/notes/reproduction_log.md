# baseline 复现记录

这份记录就是「如何复现 baseline」的答案来源，新人照此顺序执行即可。

## 步骤

1. 环境准备：Python 3.11，`uv sync`（依赖用 uv 锁定，见 `configs/baseline.yaml` 的 environment 段）。
2. 检查数据：确认 `/data/aurora/v3` 可读，且 `/data/aurora/cache/` 为空。缓存非空时先按
   `notes/handoff.md` 的「已知数据缓存问题」处理，否则指标必然对不上。
3. 训练：`python code/train.py --config configs/baseline.yaml`
   （learning_rate 3.0e-4、batch_size 128、epochs 40、seed 2026 全部来自配置文件）。
4. 评估：`python code/evaluate.py --config configs/baseline.yaml --checkpoint checkpoints/best.pt`
5. 核对：把测试集 AUROC 与 F1 和 `experiments/metrics_baseline.md` 的历史指标对照，
   落在 AUROC 0.905–0.919、F1 0.838–0.852 即视为复现成功。

## 排查清单（指标不达标时按顺序看）

| 现象                    | 最可能原因                     | 处理方式                             |
| ----------------------- | ------------------------------ | ------------------------------------ |
| AUROC 掉到 0.87 左右    | `/data/aurora/cache/` 非空     | 清空缓存后重跑，不要改模型           |
| F1 偏低、召回率明显下降 | threshold 被改动               | 恢复 `evaluation.threshold: 0.5`     |
| 训练一开始就报错        | 数据版本不是 v3                | 检查 `data.version`，v4 尚无对照实验 |
| 指标抖动但均值接近      | 用了非默认随机种子             | 属于未完成工作，见 handoff 的下一步   |

## 复现耗时

单卡 cuda:0 约 1.5 小时；排队与资源申请规则见 `ops/lab_server_resources.md`。
