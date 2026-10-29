"""Aurora 时序异常检测 —— baseline 训练入口。

复现命令写在 `notes/reproduction_log.md`，实验配置在 `configs/baseline.yaml`，
历史指标在 `experiments/metrics_baseline.md`。本文件只保留接手新人需要读懂的最小骨架；
数据位置与服务器使用规则见 `ops/lab_server_resources.md`。

    python code/train.py --config configs/baseline.yaml

`--config` 是唯一入口参数：学习率、随机种子、数据版本都来自配置文件，不要用命令行覆盖，
否则历史指标对不上。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DATA_ROOT = "/data/aurora"
DEFAULT_SEED = 2026


def load_config(path: Path) -> dict[str, Any]:
    """读取 baseline 配置。真实项目用 yaml.safe_load，这里保持同样的字段约定。"""
    return {
        "data_version": "v3",
        "learning_rate": 3e-4,
        "batch_size": 128,
        "epochs": 40,
        "seed": DEFAULT_SEED,
        "threshold": 0.5,
    }


def build_dataloader(config: dict[str, Any]) -> tuple[int, int]:
    """返回 (train_steps, val_steps)。

    数据放在共享存储 `/data/aurora/v3`。注意：历史遗留的 cache 目录会改变采样顺序，
    复现前必须先确认缓存已清理，见 `notes/handoff.md` 的「已知数据缓存问题」。
    """
    total = 24000
    batch = int(config["batch_size"])
    return int(total * 0.8 // batch), int(total * 0.2 // batch)


def train(config: dict[str, Any]) -> dict[str, float]:
    """训练占位实现：真实项目在这里跑满 epochs 个 epoch。"""
    train_steps, val_steps = build_dataloader(config)
    return {
        "train_steps": float(train_steps),
        "val_steps": float(val_steps),
        "seed": float(DEFAULT_SEED),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Aurora baseline 训练入口")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    print(train(load_config(args.config)))


if __name__ == "__main__":
    main()
