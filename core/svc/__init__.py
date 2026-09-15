"""游戏逻辑层（facade）：`GameService` 由各域 Mixin 组合。

拆分自原单体 `core/service.py`（1500+ 行），对外行为与 import 路径完全不变：

    from core.service import GameService   # 仍然可用
    from .service import GameService       # 仍然可用

模块划分（各域之间没有跨节私有状态，组合成同一个类后共享 self.db/config/copy）：

    _const.py     数值常量 + 纯函数工具（_fmt/_cd_text/_sample/_now/_iso_week/_schema_meta）
    _base.py      配置读取 / 冷却 / 玩家访问 / 主奴关系工具（_num/_int/_cd_left/_name/...）
    _work.py      打工
    _purchase.py  购买 / 放生 / 赎身
    _rob.py       抢劫
    _train.py     训练 / 一键训练
    _arena.py     决斗
    _rank.py      排位赛
    _bank.py      银行与转账
    _board.py     查询与排行榜
    _backup.py    备份
    _webapi.py    WebUI 数据接口

所有对外方法均为纯异步；返回统一为 result.R 结构：
- err：用户可见错误（优先输出）
- tmpl+data：HTML 模板渲染（Playwright）
- text：纯文本回退（渲染失败/关闭图片时使用）

事务约定（重要）：
    任何会改动金币/身价/主奴关系的操作都必须写成 `def _xxx(self, tx, ...)` 同步函数，
    再由 `db.transact()` 在单线程、单锁、单事务里执行。tx.get(uid) 取存档、原地改，
    事务结束时只写回真正变化的行。**tx 内禁止 await**（会与自身持有的锁互等）。

内部异常向上抛出，由 handlers/main 统一捕获。
"""

from __future__ import annotations

from ._arena import _ArenaMixin
from ._backup import _BackupMixin
from ._bank import _BankMixin
from ._base import _BaseMixin
from ._board import _BoardMixin
from ._const import (
    BOARD_LIMIT as BOARD_LIMIT,
)
from ._const import (
    FULL_SCAN_CAP as FULL_SCAN_CAP,
)
from ._const import (
    MARKET_LIMIT as MARKET_LIMIT,
)
from ._const import (
    MAX_AUTO_UPGRADES as MAX_AUTO_UPGRADES,
)
from ._const import (
    _cd_text as _cd_text,
)
from ._const import (
    _fmt as _fmt,
)
from ._const import (
    _iso_week as _iso_week,
)
from ._const import (
    _now as _now,
)
from ._const import (
    _sample as _sample,
)
from ._const import (
    _schema_meta as _schema_meta,
)
from ._const import (
    set_ui_texts as set_ui_texts,
)
from ._purchase import _PurchaseMixin
from ._rank import _RankMixin
from ._rob import _RobMixin
from ._train import _TrainMixin
from ._webapi import _WebApiMixin
from ._work import _WorkMixin


class GameService(
    _BaseMixin,
    _WorkMixin,
    _PurchaseMixin,
    _RobMixin,
    _TrainMixin,
    _ArenaMixin,
    _RankMixin,
    _BankMixin,
    _BoardMixin,
    _BackupMixin,
    _WebApiMixin,
):
    """游戏逻辑层：各域 Mixin 组合，共享同一个 db / config / copy。"""

    def __init__(self, db, config: dict, copywriting: dict):
        self.db = db
        self.config = config
        self.copy = copywriting


__all__ = ["GameService"]
