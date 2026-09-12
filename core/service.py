"""游戏逻辑层：对外入口（facade）。

实现已按域拆分到 `core/svc/` 包，本模块只保留重新导出，
保证既有 import 路径继续可用：

    from core.service import GameService
    from core.service import _cd_text, _iso_week, _schema_meta   # 工具函数

（这些名字在拆分前就是本模块的模块级名字，历史代码/脚本可能直接引用。）
"""

from __future__ import annotations

from .svc import (
    BOARD_LIMIT,
    FULL_SCAN_CAP,
    MARKET_LIMIT,
    MAX_AUTO_UPGRADES,
    GameService,
    _cd_text,
    _fmt,
    _iso_week,
    _now,
    _sample,
    _schema_meta,
)

__all__ = [
    "BOARD_LIMIT",
    "FULL_SCAN_CAP",
    "MARKET_LIMIT",
    "MAX_AUTO_UPGRADES",
    "GameService",
    "_cd_text",
    "_fmt",
    "_iso_week",
    "_now",
    "_sample",
    "_schema_meta",
]
