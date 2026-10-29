"""Repository-relative paths the API and the demo share."""

from __future__ import annotations

import os
from pathlib import Path

#: ``qoder/labrelay`` — the directory that owns ``configs/``, ``data/`` and ``backend/``.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def data_root() -> Path:
    """The local data directory, resolved against the project so uploads stay inside it.

    ``LABRELAY_DATA_ROOT`` may be absolute or relative to :data:`PROJECT_ROOT`; the default
    ``data/`` is the directory the repository's ignore rules already cover.
    """
    configured = os.getenv("LABRELAY_DATA_ROOT", "data").strip() or "data"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()
