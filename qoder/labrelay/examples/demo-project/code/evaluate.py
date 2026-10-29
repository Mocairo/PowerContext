"""Aurora 时序异常检测 —— 评估入口。

训练完成后运行：

    python code/evaluate.py --config configs/baseline.yaml --checkpoint checkpoints/best.pt

评估只读取 `configs/baseline.yaml` 里的 `data_version` 和 `threshold`，
任何一项改动都会让 AUROC 与历史指标不一致，历史指标见 `experiments/metrics_baseline.md`。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from train import load_config

#: baseline 在 v3 测试集上的历史结果，接手后应能复现到同样的数量级。
EXPECTED_AUROC = 0.912
EXPECTED_F1 = 0.845


def evaluate(config: dict[str, Any]) -> dict[str, float]:
    """评估占位实现：真实项目在这里加载 checkpoint 并计算 AUROC / F1。"""
    return {"auroc": EXPECTED_AUROC, "f1": EXPECTED_F1, "threshold": float(config["threshold"])}


def main() -> None:
    parser = argparse.ArgumentParser(description="Aurora baseline 评估入口")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    args = parser.parse_args()
    print(evaluate(load_config(args.config)))


if __name__ == "__main__":
    main()
