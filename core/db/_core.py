"""连接、生命周期与事务执行器（拆分自原 core/db.py）。

- 每条同步方法用一条**短生命周期**连接；`_connect()` 每次新建。
- `transact()` 在同一线程 / 同一锁 / 同一事务内完成"读→改→写"，
  杜绝 load 与 save 之间被其它指令插入导致的整行覆盖与丢失更新。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ._const import (
    _NICK_MAX,
    _SCHEMA,
    _UPSERT,
    _player_to_args,
    _rank_init_from_tiers,
    _to_int,
    new_player,
)

if TYPE_CHECKING:  # 仅用于类型标注，避免与 __init__.py 形成导入环
    from . import PlayerDB


class Abort(Exception):
    """在 transact() 内主动放弃本次事务，并把 result 作为返回值交回调用方。

    用于「校验不通过 → 不写任何数据，直接返回提示」的分支。
    """

    def __init__(self, result=None):
        super().__init__("transaction aborted")
        self.result = result


class Txn:
    """transact() 事务内的玩家视图。

    get() 读到的 dict 可以随意原地修改；事务正常结束时，**只有内容真的发生变化**
    的玩家才会被写回（避免只读参与者被整行重写、也避免给未注册用户凭空建档）。
    """

    def __init__(self, db: PlayerDB, conn: sqlite3.Connection, gid: str):
        self._db = db
        self._conn = conn
        self._gid = gid
        self._cache: dict[str, dict] = {}
        self._snap: dict[str, dict] = {}
        self._exists: dict[str, bool] = {}
        self._nick: dict[str, str] = {}

    def get(self, uid: str, nickname: str | None = None) -> dict:
        uid = str(uid)
        if uid not in self._cache:
            data, found = self._db._read_row(self._conn, self._gid, uid)
            self._exists[uid] = found
            self._cache[uid] = data
            self._snap[uid] = copy.deepcopy(data)
        if nickname:
            # 昵称只是显示优化，绝不能仅因为它就触发写入：否则一条
            # 「训练 @从没玩过的人」在校验失败返回后，仍会给对方建档，
            # 幽灵玩家又会出现在市场与排行榜里。落库判断留给 _flush。
            self._nick[uid] = str(nickname)[:_NICK_MAX]
        return self._cache[uid]

    def exists(self, uid: str) -> bool:
        """该 uid 在本群是否已有存档行（未注册用户返回 False）。"""
        self.get(uid)
        return self._exists[str(uid)]

    def abort(self, result=None):
        raise Abort(result)

    def _flush(self) -> int:
        written = 0
        for uid, data in self._cache.items():
            # 取证过的坏行（broken=1）禁止被 UPSERT 覆盖：源行的损坏 JSON 是唯一
            # 现场，让人工从 trash 还原是合法的恢复路径；一旦被覆写成合法 []，
            # 就再也无法"还原到损坏前的样子"。
            # 必须直接查源行的 broken 列（_read_row 把它降级成默认 0 的
            # new_player 后，snap['broken'] 永远是 0，不能作为判据）。
            row = self._conn.execute(
                "SELECT broken FROM players WHERE gid=? AND uid=?",
                (self._gid, uid),
            ).fetchone()
            if row and row["broken"] == 1:
                continue
            snap = self._snap[uid]
            changed = data != snap
            nick = self._nick.get(uid)
            if nick and (changed or self._exists[uid]) and data["nickname"] != nick:
                data["nickname"] = nick
                changed = True
            if not changed:
                continue
            self._conn.execute(_UPSERT, _player_to_args(self._gid, uid, data))
            written += 1
        return written

class _CoreMixin:
    """异步门面 + 同步 sqlite3 内核。"""

    def __init__(self, db_path: Path, backup_keep: int = 10, bank_init=None):
        self.path = Path(db_path)
        self.backup_keep = max(0, int(backup_keep))
        self._bank_init: dict[str, int] = {}
        self.set_bank_init(bank_init)
        # 新号初始段位：默认 None = 用 _const.NEW_PLAYER 的静态默认值。
        # 运行期由 GameCtx.set_copywriting() 按用户自定义段位表首档热更新。
        self._rank_init: tuple[int, str] | None = None
        self._backup_root = self.path.parent / "backups"
        self._lock = threading.RLock()
        self._closed = False
        # in-flight to_thread worker 计数：close() 在持锁时仍然会等它们自己释放
        # （因为 RLock 同一线程可重入、worker 退出后 _lock 真正空闲）。
        # close 之前先检查是否有 worker 仍在跑，给两次重试，
        # 避免 close 与 worker 并发导致 worker 撞到 ProgrammingError。
        self._inflight = 0
        self._inflight_cv = threading.Condition(self._lock)

    def set_bank_init(self, bank_cfg: dict | None) -> None:
        """热更新新玩家的初始银行参数（WebUI 改配置后调用）。"""
        cfg = bank_cfg or {}
        out = {}
        for key, src in (
            ("level", "initialLevel"),
            ("limit", "initialLimit"),
            ("upgradePrice", "initialUpgradePrice"),
        ):
            if src in cfg:
                v = _to_int(cfg.get(src), 0)
                if v > 0:
                    out[key] = v
        self._bank_init = out

    def set_rank_init(self, tiers) -> None:
        """热更新新玩家的初始分数/段位（gameTexts.ranking_tiers 首档）。

        用户把段位表改成 500 起步后，新号必须是 499/首档名（门槛是
        svc._tier() 里「离开该档」的上界，500 已属下一档），而不是库里写死的
        999/「青铜」。
        只影响**建号默认值**：已有存档的分数/段位以库里的行为准，
        `_sanitize()` 也仍然只认 _const 的静态默认值，不依赖运行期文案。
        """
        self._rank_init = _rank_init_from_tiers(tiers)

    def new_player(self) -> dict:
        """新玩家数据（应用配置里的初始银行参数与段位表首档）。"""
        d = new_player()
        d["bank"].update(self._bank_init)
        if self._rank_init is not None:
            d["ranking"]["score"], d["ranking"]["tier"] = self._rank_init
        return d

    # ---------- 连接 ----------

    def _connect(self) -> sqlite3.Connection:
        """打开一条**短生命周期**连接（仅当前线程/同步方法内使用）。

        每次调用新开一条连接，配合 to_thread 池安全使用：
        - `timeout=15` 给 SQLITE_BUSY 一个折中的等待窗口
        - 行工厂 Row 每次都设（小开销，换来稳定访问）
        - WAL 模式每次都启用（持久生效）
        """
        if self._closed:
            raise sqlite3.ProgrammingError("数据库已关闭")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _close(self) -> None:
        """空操作。`_closed` 标志由 `_backup.py` 的关闭流程置位。

        本类不持有长生命周期连接：每条 sync 方法用完即关（`_connect()` 每次新建），
        因此这里没有需要 close 的实例连接。
        """
        return None

    @contextlib.contextmanager
    def _inflight_guard(self):
        """在持锁临界区里给 _inflight +1 / -1，让 close() 能等到 worker 退出。

        必须配合 `with self._lock:` 一起用：cv 与 lock 共用同一个 RLock，
        否则 cv.wait() 会死锁在错误的 monitor 上。
        """
        self._inflight += 1
        try:
            yield
        finally:
            with self._inflight_cv:
                self._inflight -= 1
                if self._inflight == 0:
                    self._inflight_cv.notify_all()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                # 建表只以 _SCHEMA 为准（CREATE TABLE IF NOT EXISTS），
                # 不做任何旧库补列/迁移：列集与 _SCHEMA 不一致的存档会在
                # 首次读写时抛出带列名的清晰错误，需删除旧 db 让插件重建。
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    async def transact(self, group_id: str, fn) -> object:
        """在单线程、单锁、单事务内完成一次跨玩家结算。

        fn 形如 `def fn(tx: Txn): ...`，用 `tx.get(uid)` 取存档并原地修改；
        校验不通过时调用 `tx.abort(结果)` 放弃写入。fn 内**不得**再 await 或调用
        本类的 async 方法（会造成锁重入等待自身）。
        """
        return await asyncio.to_thread(self._transact_sync, str(group_id), fn)

    def _transact_sync(self, gid: str, fn) -> object:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                tx = Txn(self, conn, gid)
                try:
                    result = fn(tx)
                    tx._flush()
                    conn.commit()
                    return result
                except Abort as stop:
                    conn.rollback()
                    return stop.result
                except Exception:
                    conn.rollback()
                    raise
            finally:
                conn.close()

