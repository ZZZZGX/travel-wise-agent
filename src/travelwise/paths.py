# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""统一路径解析。

原实现用 `os.path.dirname(__file__)/../data` 定位数据，隐含了"脚本必须待在
特定目录深度"的假设，一旦挪动文件就断。这里集中解析一次：

  优先环境变量 TRAVELWISE_DATA_DIR（部署时可任意指定）
  否则回退到仓库根的 data/（src/travelwise/paths.py → 上溯三级）
"""

from __future__ import annotations

import os
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent          # src/travelwise
PROJECT_ROOT = _PKG_DIR.parent.parent               # 仓库根

DATA_DIR = Path(os.environ.get("TRAVELWISE_DATA_DIR", PROJECT_ROOT / "data"))
CONFIG_DIR = Path(os.environ.get("TRAVELWISE_CONFIG_DIR", PROJECT_ROOT / "config"))
CACHE_DIR = DATA_DIR / "cache"


def ensure_cache_dir(*parts: str) -> Path | None:
    """确保并返回一个缓存子目录；**不可用时返回 None**。

    原实现在 mkdir 失败后仍返回 Path，调用方会以为目录可用，
    于是把"缓存写不进去"变成后续某处莫名其妙的写失败。
    现在把不可用显式表达出来：缓存是可选优化，调用方拿到 None
    应当跳过缓存继续跑，而不是崩掉，也不是以为写成功了。
    """
    p = CACHE_DIR.joinpath(*parts)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return p if os.access(p, os.W_OK) else None
