"""SQLite 玩家数据存储层（facade）：`PlayerDB` 由各域 Mixin 组合。

拆分自原单体 `core/db.py`（1100+ 行），对外行为与 import 路径完全不变：

    from core.db import PlayerDB        # 仍然可用
    from .db import PlayerDB, _NUM_CAP  # 仍然可用

模块划分（各域之间没有跨节私有状态，组合成同一个类后共享 `self._lock`）：

    _const.py      常量 / DDL / 默认值解析 / 行↔玩家数据转换
    _core.py       连接、短生命周期管理、in-flight 计数、事务执行器（Abort/Txn）
    _players.py    玩家级读写：load / exists / delete（含 trash 留档与引用清理）/ set_card
    _group.py      群级聚合：list_players / group_counts / query_* / search_players / totals
    _backup.py     全量备份 / 恢复 / 裁剪 / 关闭

存储层总说明（同原 db.py 模块 docstring）：

- 单文件库 `slave_market.db`，WAL 模式；所有磁盘操作经 asyncio.to_thread，不阻塞事件循环。
- 同步内部方法用 threading.RLock 串行化（to_thread 可能并发进入不同线程）。
- 跨玩家结算必须走 `transact()`：整段「读→改→写」在同一线程、同一锁、同一事务内完成，
  杜绝「load 与 save 之间被其他指令插入」导致的整行覆盖与丢失更新。
- 备份用 sqlite3.Connection.backup() 生成一致性快照；恢复前校验来源库并自动留一份保命快照。
- 删除存档前整行挪入 trash 留档，并清理其他玩家对该 uid 的主/奴引用。
- 新号默认值（含银行初始额度、段位初值）的权威来源是 `_conf_schema.json` 与
  `resources/data/gameTexts.json`，见 `_const.py` 的 `_resolve_defaults()`。
"""

from __future__ import annotations

from ._backup import _BackupMixin as _BackupMixin
from ._const import (
    _BANK_DEFAULTS as _BANK_DEFAULTS,
)
from ._const import (
    _NUM_CAP as _NUM_CAP,
)
from ._const import (
    _RANK_DEFAULT_SCORE as _RANK_DEFAULT_SCORE,
)
from ._const import (
    _RANK_DEFAULT_TIER as _RANK_DEFAULT_TIER,
)
from ._const import (
    _SCHEMA as _SCHEMA,
)
from ._const import (
    _VALUE_DEFAULT as _VALUE_DEFAULT,
)
from ._const import (
    NEW_PLAYER as NEW_PLAYER,
)
from ._const import (
    _escape_like as _escape_like,
)
from ._const import (
    _player_to_args as _player_to_args,
)
from ._const import (
    _read_schema as _read_schema,
)
from ._const import (
    _resolve_defaults as _resolve_defaults,
)
from ._const import (
    _row_to_player as _row_to_player,
)
from ._const import (
    _sanitize as _sanitize,
)
from ._const import (
    _to_float as _to_float,
)
from ._const import (
    _to_int as _to_int,
)
from ._const import (
    new_player as new_player,
)
from ._core import Abort as Abort
from ._core import Txn as Txn
from ._core import _CoreMixin as _CoreMixin
from ._group import _GroupMixin as _GroupMixin
from ._players import _PlayersMixin as _PlayersMixin


class PlayerDB(_CoreMixin, _PlayersMixin, _GroupMixin, _BackupMixin):
    """异步门面 + 同步 sqlite3 内核。

    Mixin 组合顺序只影响方法解析顺序；四个 Mixin 之间没有同名方法。
    """


__all__ = [
    "NEW_PLAYER",
    "Abort",
    "PlayerDB",
    "Txn",
    "new_player",
]
