"""SQLite 玩家数据存储层。

- 单文件库 `slave_market.db`，WAL 模式；所有磁盘操作经 asyncio.to_thread，不阻塞事件循环。
- 同步内部方法用 threading.RLock 串行化（to_thread 可能并发进入不同线程）。
- 跨玩家结算必须走 `transact()`：整段「读→改→写」在同一线程、同一锁、同一事务内完成，
  杜绝「load 与 save 之间被其他指令插入」导致的整行覆盖与丢失更新。
- 备份用 `VACUUM INTO` 生成一致性快照；恢复前校验来源库并自动留一份保命快照。
- 删除存档前整行挪入 trash 留档，并清理其他玩家对该 uid 的主/奴引用。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("astrbot")

# 昵称最大存储长度（防止异常输入把行撑大）
_NICK_MAX = 64
# 回收站保留行数：与"备份文件份数"解耦，否则删档留档与坏行取证会互相挤掉
_TRASH_KEEP = 200
# 数值硬顶：SQLite INTEGER 是 64 位，超出会让 conn.execute 抛 OverflowError
_NUM_CAP = 2**62
# 备份文件名前缀；恢复前的保命快照另用前缀并放进子目录，不参与常规备份列表与裁剪
_BACKUP_PREFIX = "slave_market_"
_PRERESTORE_DIR = "prerestore"
_PRERESTORE_KEEP = 5

# 新玩家的初始数据模板（唯一模板，读取时必须经 new_player() 深拷贝）
NEW_PLAYER = {
    "currency": 0.0,
    "value": 100.0,
    "master": "",
    "slave": [],
    "nickname": "",
    "lastWorkingTime": 0,
    "lastPurchaseTime": 0,
    "lastRobTime": 0,
    "lastBuyBackTime": 0,
    "buyBackTimes": 0,
    "lastBattleTime": 0,
    "battleStats": {"wins": 0, "losses": 0},
    "lastTrainedTime": 0,
    "lastRankingTime": 0,
    "ranking": {"score": 1000, "tier": "青铜", "matches": 0},
    "bank": {
        "balance": 0.0,
        "level": 1,
        "limit": 1000,
        "upgradePrice": 100,
        "lastInterestTime": 0,
    },
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    gid                 TEXT    NOT NULL,
    uid                 TEXT    NOT NULL,
    nickname            TEXT    DEFAULT '',
    currency            REAL    DEFAULT 0,
    value               REAL    DEFAULT 100,
    master              TEXT    DEFAULT '',
    slave               TEXT    DEFAULT '[]',
    last_working_time   INTEGER DEFAULT 0,
    last_purchase_time  INTEGER DEFAULT 0,
    last_rob_time       INTEGER DEFAULT 0,
    last_buyback_time   INTEGER DEFAULT 0,
    buyback_times       INTEGER DEFAULT 0,
    last_battle_time    INTEGER DEFAULT 0,
    battle_wins         INTEGER DEFAULT 0,
    battle_losses       INTEGER DEFAULT 0,
    last_trained_time   INTEGER DEFAULT 0,
    last_ranking_time   INTEGER DEFAULT 0,
    rank_score          INTEGER DEFAULT 1000,
    rank_tier           TEXT    DEFAULT '青铜',
    rank_matches        INTEGER DEFAULT 0,
    bank_balance        REAL    DEFAULT 0,
    bank_level          INTEGER DEFAULT 1,
    bank_limit          INTEGER DEFAULT 1000,
    bank_upgrade_price  INTEGER DEFAULT 100,
    bank_last_interest  INTEGER DEFAULT 0,
    updated_at          INTEGER DEFAULT 0,
    broken              INTEGER DEFAULT 0,
    PRIMARY KEY (gid, uid)
);
CREATE TABLE IF NOT EXISTS trash (
    gid                 TEXT    NOT NULL,
    uid                 TEXT    NOT NULL,
    row_data            TEXT    NOT NULL,
    deleted_at          INTEGER NOT NULL
);
"""


def new_player() -> dict:
    """返回全新玩家数据（深拷贝模板）。

    必须深拷贝：浅拷贝会让多个未落库的新玩家共享同一组嵌套对象
    （bank/ranking/battleStats/slave），任何原地修改都会污染模块级模板。
    """
    return copy.deepcopy(NEW_PLAYER)


def _to_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        # NaN 与 ±inf 都必须挡住：inf 会污染余额并让 json.dumps 产出非法 JSON
        if not math.isfinite(f):
            return default
        # 同时夹住量级：过大的值写库时会溢出，也会让后续计算失去意义
        return max(-float(_NUM_CAP), min(float(_NUM_CAP), f))
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int = 0) -> int:
    """容错整型转换：非法值回退默认值，并夹到 SQLite 能存的范围内。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return int(max(-_NUM_CAP, min(_NUM_CAP, f)))


def _uid_key(uid: str):
    """奴隶 id 排序键：纯数字按数值排，其余按字典序排在后面。"""
    s = str(uid)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def _sanitize(data: dict) -> dict:
    """规范化玩家数据，修复缺失/非法字段。"""
    merged = copy.deepcopy(NEW_PLAYER)
    for k in NEW_PLAYER:
        if k in data:
            merged[k] = data[k]

    merged["currency"] = max(0.0, _to_float(merged.get("currency")))
    merged["value"] = max(0.0, _to_float(merged.get("value"), 100.0))

    # 奴隶 id 统一按字符串存：平台 uid 不保证是纯数字（Discord/KOOK 等）
    merged["slave"] = sorted(
        {str(s) for s in merged.get("slave") or [] if str(s).strip()}, key=_uid_key
    )

    master = merged.get("master")
    merged["master"] = str(master) if master not in ("", None) else ""

    nickname = merged.get("nickname")
    merged["nickname"] = ("" if nickname is None else str(nickname))[:_NICK_MAX]

    stats = merged.get("battleStats") or {}
    merged["battleStats"] = {
        "wins": max(0, _to_int(stats.get("wins"))),
        "losses": max(0, _to_int(stats.get("losses"))),
    }

    ranking = merged.get("ranking") or {}
    merged["ranking"] = {
        "score": _to_int(ranking.get("score"), 1000),
        "tier": str(ranking.get("tier") or "青铜"),
        "matches": max(0, _to_int(ranking.get("matches"))),
    }

    bank = merged.get("bank") or {}
    merged["bank"] = {
        "balance": max(0.0, _to_float(bank.get("balance"))),
        "level": max(1, _to_int(bank.get("level"), 1)),
        "limit": max(1, _to_int(bank.get("limit"), 1000)),
        "upgradePrice": max(1, _to_int(bank.get("upgradePrice"), 100)),
        "lastInterestTime": max(0, _to_int(bank.get("lastInterestTime"))),
    }

    for key in (
        "lastWorkingTime",
        "lastPurchaseTime",
        "lastRobTime",
        "lastBuyBackTime",
        "buyBackTimes",
        "lastBattleTime",
        "lastTrainedTime",
        "lastRankingTime",
    ):
        merged[key] = max(0, _to_int(merged.get(key)))

    return merged


_COLS = [
    "gid",
    "uid",
    "nickname",
    "currency",
    "value",
    "master",
    "slave",
    "last_working_time",
    "last_purchase_time",
    "last_rob_time",
    "last_buyback_time",
    "buyback_times",
    "last_battle_time",
    "battle_wins",
    "battle_losses",
    "last_trained_time",
    "last_ranking_time",
    "rank_score",
    "rank_tier",
    "rank_matches",
    "bank_balance",
    "bank_level",
    "bank_limit",
    "bank_upgrade_price",
    "bank_last_interest",
]

# 列 -> DDL 片段，供 _migrate() 给旧库补列。键名与 _SCHEMA 中的列名一一对应；
# _migrate 用 PRAGMA table_info(players) 拿到缺列名后逐项 ALTER TABLE 补建，
# 不依赖 DDL 字符串与 _SCHEMA 完全相同——加新列时只需在这里登记即可。
_COL_DDL = {
    "nickname": "TEXT DEFAULT ''",
    "currency": "REAL DEFAULT 0",
    "value": "REAL DEFAULT 100",
    "master": "TEXT DEFAULT ''",
    "slave": "TEXT DEFAULT '[]'",
    "last_working_time": "INTEGER DEFAULT 0",
    "last_purchase_time": "INTEGER DEFAULT 0",
    "last_rob_time": "INTEGER DEFAULT 0",
    "last_buyback_time": "INTEGER DEFAULT 0",
    "buyback_times": "INTEGER DEFAULT 0",
    "last_battle_time": "INTEGER DEFAULT 0",
    "battle_wins": "INTEGER DEFAULT 0",
    "battle_losses": "INTEGER DEFAULT 0",
    "last_trained_time": "INTEGER DEFAULT 0",
    "last_ranking_time": "INTEGER DEFAULT 0",
    "rank_score": "INTEGER DEFAULT 1000",
    "rank_tier": "TEXT DEFAULT '青铜'",
    "rank_matches": "INTEGER DEFAULT 0",
    "bank_balance": "REAL DEFAULT 0",
    "bank_level": "INTEGER DEFAULT 1",
    "bank_limit": "INTEGER DEFAULT 1000",
    "bank_upgrade_price": "INTEGER DEFAULT 100",
    "bank_last_interest": "INTEGER DEFAULT 0",
    "updated_at": "INTEGER DEFAULT 0",
    "broken": "INTEGER DEFAULT 0",  # 1 = 已取证到 trash；后续 _read_row 不再写
}
# bad-row 一旦留档就置 1，避免 _read_row 每次都往 trash 里再插一份

# query_players(order_by=...) 允许的排序列白名单（防止列名拼接进 SQL）
_SORTABLE = {
    "uid",
    "nickname",
    "currency",
    "value",
    "rank_score",
    "bank_balance",
    "bank_level",
    "updated_at",
}


def _row_to_player(row: sqlite3.Row) -> dict:
    return _sanitize(
        {
            "nickname": row["nickname"],
            "currency": row["currency"],
            "value": row["value"],
            "master": row["master"],
            "slave": json.loads(row["slave"] or "[]"),
            "lastWorkingTime": row["last_working_time"],
            "lastPurchaseTime": row["last_purchase_time"],
            "lastRobTime": row["last_rob_time"],
            "lastBuyBackTime": row["last_buyback_time"],
            "buyBackTimes": row["buyback_times"],
            "lastBattleTime": row["last_battle_time"],
            "battleStats": {"wins": row["battle_wins"], "losses": row["battle_losses"]},
            "lastTrainedTime": row["last_trained_time"],
            "lastRankingTime": row["last_ranking_time"],
            "ranking": {
                "score": row["rank_score"],
                "tier": row["rank_tier"],
                "matches": row["rank_matches"],
            },
            "bank": {
                "balance": row["bank_balance"],
                "level": row["bank_level"],
                "limit": row["bank_limit"],
                "upgradePrice": row["bank_upgrade_price"],
                "lastInterestTime": row["bank_last_interest"],
            },
        }
    )


def _player_to_args(gid: str, uid: str, data: dict) -> tuple:
    d = _sanitize(data)
    now = int(time.time())
    return (
        str(gid),
        str(uid),
        d["nickname"],
        d["currency"],
        d["value"],
        d["master"],
        json.dumps(d["slave"]),
        d["lastWorkingTime"],
        d["lastPurchaseTime"],
        d["lastRobTime"],
        d["lastBuyBackTime"],
        d["buyBackTimes"],
        d["lastBattleTime"],
        d["battleStats"]["wins"],
        d["battleStats"]["losses"],
        d["lastTrainedTime"],
        d["lastRankingTime"],
        d["ranking"]["score"],
        d["ranking"]["tier"],
        d["ranking"]["matches"],
        d["bank"]["balance"],
        d["bank"]["level"],
        d["bank"]["limit"],
        d["bank"]["upgradePrice"],
        d["bank"]["lastInterestTime"],
        now,
    )


_UPSERT = (
    f"INSERT OR REPLACE INTO players ({', '.join(_COLS)}, updated_at) "
    f"VALUES ({', '.join('?' for _ in range(len(_COLS) + 1))})"
)


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

    def __init__(self, db: "PlayerDB", conn: sqlite3.Connection, gid: str):
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


class PlayerDB:
    """异步门面 + 同步 sqlite3 内核。"""

    def __init__(self, db_path: Path, backup_keep: int = 10, bank_init=None):
        self.path = Path(db_path)
        self.backup_keep = max(0, int(backup_keep))
        self._bank_init: dict[str, int] = {}
        self.set_bank_init(bank_init)
        self._backup_root = self.path.parent / "backups"
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False
        # in-flight to_thread worker 计数：close() 在持锁时仍然会等它们自己释放
        # （因为 RLock 同一线程可重入、worker 退出后 _lock 真正空闲），
        # 但旧实现的问题不是死锁，是"close 与 worker 并发"导致 worker 撞到
        # ProgrammingError。close 之前先看看有没有 worker 仍在跑，给两次重试。
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

    def new_player(self) -> dict:
        """新玩家数据（应用配置里的初始银行参数）。"""
        d = new_player()
        d["bank"].update(self._bank_init)
        return d

    # ---------- 连接 ----------

    def _connect(self) -> sqlite3.Connection:
        """打开一条**短生命周期**连接（仅当前线程/同步方法内使用）。

        早期版本用一个 `self._conn` 长连接 + `check_same_thread=False`，
        但 to_thread 池里多个 worker 会同时操作同一条连接，
        一旦 `_close()` 被任何线程触发，其他 worker 立即抛
        "Cannot operate on a closed database"。
        长连接还会在 WAL 重连、热重载时被锁在旧文件上。

        现在改为每次调用 `_connect` 新开一条：
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
        """把长连接缓存置为关闭：实际 SQLITE 连接由各 sync 方法用完自己关。

        现在 `_connect` 每次新建短连接，本方法主要是把 `_closed` 置位让后续
        `_connect()` 立即报错。多线程 to_thread 不再被共享 conn 卡死。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

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
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    conn.executescript(_SCHEMA)
                    self._migrate(conn)
                    conn.commit()
                finally:
                    conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """补齐旧库缺失的列：老存档不会因为新版本加字段而整行解析失败。"""
        have = {r["name"] for r in conn.execute("PRAGMA table_info(players)")}
        for col, ddl in _COL_DDL.items():
            if col not in have:
                conn.execute(f"ALTER TABLE players ADD COLUMN {col} {ddl}")
                logger.info("[slave_market] 存档表已补列：%s", col)

    # ---------- 玩家级操作 ----------

    def _read_row(self, conn: sqlite3.Connection, gid: str, uid: str) -> tuple[dict, bool]:
        """读单行 -> (玩家数据, 是否已有存档行)。解析失败时先留档再降级为新号。

        已取证过的坏行（broken=1）直接返回空号：避免每次读都往 trash 里再插一份，
        把 `_TRASH_KEEP` 上限冲掉。
        """
        row = conn.execute(
            "SELECT * FROM players WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        if row is None:
            return self.new_player(), False
        # 取证过一次后立刻打标记：下一次 _read_row 直接跳过归档，
        # 避免 _TRASH_KEEP 上限被同一行坏数据冲掉
        if "broken" in row.keys() and row["broken"]:
            return self.new_player(), True
        try:
            return _row_to_player(row), True
        except Exception as e:  # noqa: BLE001 - 含 IndexError（缺列）等一切解析异常
            logger.error("[slave_market] 存档行解析失败 %s/%s: %s", gid, uid, e)
            self._archive_row(conn, gid, uid, row, reason=f"parse_error: {e}")
            try:
                conn.execute(
                    "UPDATE players SET broken=1, updated_at=? WHERE gid=? AND uid=?",
                    (int(time.time()), gid, uid),
                )
            except sqlite3.Error:
                pass  # 老库若没 broken 列（升级前生成的），降级为下一轮再试
            conn.commit()
            # 已留档到 trash，可人工恢复；返回新号让指令继续走完而不是整条指令报错
            return self.new_player(), True

    def _archive_row(
        self, conn: sqlite3.Connection, gid: str, uid: str, row, reason: str = ""
    ) -> None:
        """把整行原样存进 trash（不删原行），用于删除留档与坏行取证。"""
        payload = {k: row[k] for k in row.keys()}
        if reason:
            payload["__reason__"] = reason
        conn.execute(
            "INSERT INTO trash (gid, uid, row_data, deleted_at) VALUES (?,?,?,?)",
            (gid, uid, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.execute(
            "DELETE FROM trash WHERE rowid NOT IN ("
            " SELECT rowid FROM trash ORDER BY deleted_at DESC, rowid DESC"
            " LIMIT ?)",
            (_TRASH_KEEP,),
        )

    async def transact(self, group_id: str, fn) -> object:
        """在单线程、单锁、单事务内完成一次跨玩家结算。

        fn 形如 `def fn(tx: Txn): ...`，用 `tx.get(uid)` 取存档并原地修改；
        校验不通过时调用 `tx.abort(结果)` 放弃写入。fn 内**不得**再 await 或调用
        本类的 async 方法（会造成锁重入等待自身）。
        """
        return await asyncio.to_thread(self._transact_sync, str(group_id), fn)

    def _transact_sync(self, gid: str, fn) -> object:
        with self._lock:
            with self._inflight_guard():
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

    async def exists(self, group_id: str, user_id: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, str(group_id), str(user_id))

    def _exists_sync(self, gid: str, uid: str) -> bool:
        with self._lock:
            with self._inflight_guard():
                cur = self._connect().execute(
                    "SELECT 1 FROM players WHERE gid=? AND uid=?", (gid, uid)
                )
                return cur.fetchone() is not None

    async def load(self, group_id: str, user_id: str) -> dict:
        return await asyncio.to_thread(self._load_sync, str(group_id), str(user_id))

    def _load_sync(self, gid: str, uid: str) -> dict:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    data, _found = self._read_row(conn, gid, uid)
                    conn.commit()  # _read_row 可能写过 trash 留档
                    return data
                finally:
                    conn.close()

    async def save(
        self, group_id: str, user_id: str, data: dict, nickname: str | None = None
    ) -> None:
        await asyncio.to_thread(
            self._save_sync, str(group_id), str(user_id), data, nickname
        )

    def _save_sync(self, gid: str, uid: str, data: dict, nickname: str | None) -> None:
        if nickname:
            data = {**data, "nickname": nickname}
        args = _player_to_args(gid, uid, data)
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    conn.execute(_UPSERT, args)
                    conn.commit()
                finally:
                    conn.close()

    async def delete(self, group_id: str, user_id: str) -> None:
        """删除存档：整行挪入 trash 留档，并清理其他玩家对该 uid 的主/奴引用。"""
        await asyncio.to_thread(self._delete_sync, str(group_id), str(user_id))

    def _delete_sync(self, gid: str, uid: str) -> None:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM players WHERE gid=? AND uid=?", (gid, uid)
                ).fetchone()
                if row is None:
                    return
                try:
                    self._archive_row(conn, gid, uid, row, reason="deleted")
                    conn.execute("DELETE FROM players WHERE gid=? AND uid=?", (gid, uid))
                    self._unlink_refs(conn, gid, uid)
                    conn.commit()
                except Exception:
                    conn.rollback()  # 防止部分语句残留到下一事务
                    raise

    def _unlink_refs(self, conn: sqlite3.Connection, gid: str, uid: str) -> None:
        """清理悬空引用：别人的 master 指向它、或 slave 列表里含它。"""
        now = int(time.time())
        conn.execute(
            "UPDATE players SET master='', updated_at=? WHERE gid=? AND master=?",
            (now, gid, uid),
        )
        # 不用 LIKE 预筛：slave 列是 json.dumps 的结果（ensure_ascii=True），
        # 非 ASCII 的平台 uid 在库里是 \uXXXX 转义形式，用原字符匹配会漏掉。
        # 单群行数有限，全量拉回来在 Python 里精确比对更稳。
        rows = conn.execute(
            "SELECT uid, slave FROM players WHERE gid=? AND slave<>'[]'", (gid,)
        ).fetchall()
        for r in rows:
            try:
                ids = [str(s) for s in json.loads(r["slave"] or "[]")]
            except (TypeError, ValueError) as e:
                logger.warning(
                    "[slave_market] %s/%s 的 slave 字段损坏，跳过清理：%s",
                    gid,
                    r["uid"],
                    e,
                )
                continue
            kept = [s for s in ids if s != uid]
            if len(kept) != len(ids):
                conn.execute(
                    "UPDATE players SET slave=?, updated_at=? WHERE gid=? AND uid=?",
                    (json.dumps(kept), now, gid, r["uid"]),
                )

    # ---------- 群级操作 ----------

    async def set_card(self, group_id: str, user_id: str, card: str) -> None:
        """登记平台昵称。只更新已有存档，**不为未参与游戏的人建档**。

        早期版本这里用 INSERT OR IGNORE 建档，会让任何被 @ 到的人以默认身价
        出现在市场与排行榜里（幽灵玩家），也让 exists() 无法判断"是否玩过"。
        """
        await asyncio.to_thread(self._set_card_sync, str(group_id), str(user_id), card)

    def _set_card_sync(self, gid: str, uid: str, card: str) -> None:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                conn.execute(
                    "UPDATE players SET nickname=?, updated_at=? WHERE gid=? AND uid=?",
                    (str(card or "")[:_NICK_MAX], int(time.time()), gid, uid),
                )
                conn.commit()

    async def list_players(self, group_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_players_sync, str(group_id))

    def _list_players_sync(self, gid: str) -> list[str]:
        with self._lock:
            with self._inflight_guard():
                cur = self._connect().execute(
                    "SELECT uid FROM players WHERE gid=? ORDER BY uid", (gid,)
                )
                return [r["uid"] for r in cur.fetchall()]

    async def list_groups(self) -> list[str]:
        return [g["gid"] for g in await self.group_counts()]

    async def group_counts(self) -> list[dict]:
        return await asyncio.to_thread(self._group_counts_sync)

    def _group_counts_sync(self) -> list[dict]:
        with self._lock:
            with self._inflight_guard():
                cur = self._connect().execute(
                    "SELECT gid, COUNT(*) AS n FROM players GROUP BY gid ORDER BY gid"
                )
                return [{"gid": r["gid"], "count": int(r["n"])} for r in cur.fetchall()]

    # ---------- 聚合查询（避免"列 id 再逐个 load"的 N+1） ----------

    async def count_players(self, group_id: str) -> int:
        return await asyncio.to_thread(self._count_players_sync, str(group_id))

    def _count_players_sync(self, gid: str) -> int:
        with self._lock:
            with self._inflight_guard():
                cur = self._connect().execute(
                    "SELECT COUNT(*) AS n FROM players WHERE gid=?", (gid,)
                )
                return int(cur.fetchone()["n"])

    async def query_players(
        self,
        group_id: str,
        order_by: str = "uid",
        desc: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[str, dict]]:
        """按列排序取一页玩家 -> [(uid, 玩家数据)]。order_by 只接受白名单列。"""
        return await asyncio.to_thread(
            self._query_players_sync,
            str(group_id),
            order_by,
            desc,
            limit,
            offset,
        )

    async def query_players_by_uids(
        self, group_id: str, uids: list[str]
    ) -> list[tuple[str, dict]]:
        """按 uid 列表取一次性多行：消除 ranking_show / my_slave 的 N+1 load。

        与 query_players 的差别：不排序、按 IN 顺序输出；空列表直接返回。
        """
        uids = [str(u) for u in (uids or []) if u]
        return await asyncio.to_thread(
            self._query_players_by_uids_sync, str(group_id), uids
        )

    def _query_players_by_uids_sync(
        self, gid: str, uids: list[str]
    ) -> list[tuple[str, dict]]:
        if not uids:
            return []
        # 去重保留顺序：单用户奴隶数 < 2000 远低于 SQLite IN 上限，安全。
        seen: set[str] = set()
        uniq: list[str] = []
        for u in uids:
            if u and u not in seen:
                seen.add(u)
                uniq.append(u)
        if not uniq:
            return []
        placeholders = ",".join("?" for _ in uniq)
        sql = f"SELECT uid FROM players WHERE gid=? AND uid IN ({placeholders})"
        params: list = [gid] + uniq
        out: list[tuple[str, dict]] = []
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    try:
                        out.append((row["uid"], _row_to_player(row)))
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "[slave_market] 跳过坏行 %s/%s: %s", gid, row["uid"], e
                        )
                conn.commit()
        return out

    def _query_players_sync(
        self, gid: str, order_by: str, desc: bool, limit: int | None, offset: int
    ) -> list[tuple[str, dict]]:
        col = order_by if order_by in _SORTABLE else "uid"
        sql = (
            f"SELECT * FROM players WHERE gid=? ORDER BY {col} "
            f"{'DESC' if desc else 'ASC'}, uid ASC"
        )
        params: list = [gid]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [max(0, int(limit)), max(0, int(offset))]
        out: list[tuple[str, dict]] = []
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    try:
                        out.append((row["uid"], _row_to_player(row)))
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "[slave_market] 跳过坏行 %s/%s: %s", gid, row["uid"], e
                        )
                conn.commit()
        return out

    async def totals(self) -> dict:
        """全局统计：一条 SQL 出结果，不再逐个玩家 load。"""
        return await asyncio.to_thread(self._totals_sync)

    def _totals_sync(self) -> dict:
        with self._lock:
            with self._inflight_guard():
                r = self._connect().execute(
                    "SELECT COUNT(*) AS players, COUNT(DISTINCT gid) AS groups,"
                    " COALESCE(SUM(currency),0) AS currency,"
                    " COALESCE(SUM(bank_balance),0) AS bank,"
                    " COALESCE(SUM(CASE WHEN master<>'' THEN 1 ELSE 0 END),0) AS slaves"
                    " FROM players"
                ).fetchone()
                return {
                    "players": int(r["players"]),
                    "groups": int(r["groups"]),
                    "currency": _to_float(r["currency"]),
                    "bank": _to_float(r["bank"]),
                    "slaves": int(r["slaves"]),
                }

    # ---------- 全量备份 / 恢复 ----------

    async def create_backup(self) -> str:
        return await asyncio.to_thread(self._create_backup_sync)

    def _create_backup_sync(self) -> str:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self._backup_root.mkdir(parents=True, exist_ok=True)
        with self._lock:  # 命名全程持锁，杜绝并发撞名
            with self._inflight_guard():
                dest = self._backup_root / f"{_BACKUP_PREFIX}{ts}.db"
                seq = 1
                while dest.exists():  # 同秒内多次备份时避免撞名
                    seq += 1
                    dest = self._backup_root / f"{_BACKUP_PREFIX}{ts}_{seq}.db"
                try:
                    # backup() API 在 WAL 模式下只读 main db file，不读 WAL：
                    # 刚 commit 的 999 还在 t.db-wal 里没刷到 t.db，备份会拿到 100。
                    # 所以先 TRUNCATE checkpoint，把 WAL 强制合并进主文件，
                    # 备份与恢复路径在 Windows 上也安全（Connection.backup 不
                    # 像 VACUUM INTO 那样把源库的 mmap 句柄泄漏到备份文件）。
                    chk = self._connect()
                    chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    chk.close()
                    src = self._connect()
                    dst = sqlite3.connect(dest, timeout=15)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                        src.close()
                except Exception:
                    # 失败会留下 0 字节/截断文件，且命名与正常备份无从区分，
                    # 之后"恢复备份 1"会直接踩雷 —— 必须清掉再上抛
                    dest.unlink(missing_ok=True)
                    raise
        self._prune(self._backup_root, f"{_BACKUP_PREFIX}*.db", self.backup_keep)
        return dest.name

    @staticmethod
    def _prune(root: Path, pattern: str, keep: int) -> None:
        """按修改时间保留最近 keep 个文件（keep<=0 表示不裁剪）。"""
        if keep <= 0 or not root.is_dir():
            return
        files = []
        for p in root.glob(pattern):
            try:  # glob 与 stat 之间文件可能被删，不能让已完成的备份因此报错
                files.append((p.stat().st_mtime, p))
            except OSError:
                continue
        files.sort(key=lambda t: t[0], reverse=True)
        for _mtime, old in files[keep:]:
            try:
                old.unlink()
                logger.info("[slave_market] 已裁剪旧文件 %s", old.name)
            except OSError as e:
                logger.warning("[slave_market] 裁剪失败 %s: %s", old.name, e)

    async def list_backups(self) -> list[str]:
        return await asyncio.to_thread(self._list_backups_sync)

    async def restore_backup(self, index: int) -> str | None:
        return await asyncio.to_thread(self._restore_backup_sync, int(index))

    @staticmethod
    def _verify_db_file(p: Path) -> bool:
        """恢复前校验：文件存在、能打开、完整性通过、players 表可查。"""
        if not p.is_file():
            # 不能直接 connect：sqlite 会为不存在的路径创建一个空库，
            # 校验虽然照样失败，但会在备份目录里留下垃圾文件
            logger.error("[slave_market] 备份文件不存在：%s", p.name)
            return False
        try:
            conn = sqlite3.connect(p, timeout=10)
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if str(ok).lower() != "ok":
                    logger.error("[slave_market] 备份完整性检查失败：%s", ok)
                    return False
                conn.execute("SELECT COUNT(*) FROM players").fetchone()
            finally:
                conn.close()
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[slave_market] 备份文件不可用 %s: %s", p.name, e)
            return False

    def _restore_backup_sync(self, index: int) -> str | None:
        if self._closed:
            raise sqlite3.ProgrammingError("数据库已关闭，拒绝恢复")
        with self._lock:
            with self._inflight_guard():
                # 列表与下标解析放进锁内：否则期间自动备份产生的新文件会让
                # 用户看到的序号漂移，指向另一个文件（恢复是不可逆操作）
                backups = self._list_backups_sync()
                if index < 1 or index > len(backups):
                    return None
                name = backups[index - 1]
                src_path = self._backup_root / name
                if not self._verify_db_file(src_path):
                    raise ValueError(f"备份 {name} 已损坏，已拒绝恢复")
                # 覆盖前先给当前库留一份保命快照。它放独立子目录并用独立前缀，
                # 不会与常规备份互相裁剪，也不会出现在用户可见的备份列表里。
                if self.path.exists():
                    try:
                        pre_dir = self._backup_root / _PRERESTORE_DIR
                        pre_dir.mkdir(parents=True, exist_ok=True)
                        pre_path = pre_dir / f"pre_{int(time.time())}.db"
                        # 同样走 backup() API，避免 VACUUM INTO 在 Windows 上留下 mmap 句柄
                        src_p = self._connect()
                        dst_p = sqlite3.connect(pre_path, timeout=15)
                        try:
                            src_p.backup(dst_p)
                        finally:
                            dst_p.close()
                            src_p.close()
                        self._prune(pre_dir, "pre_*.db", _PRERESTORE_KEEP)
                    except Exception as e:  # noqa: BLE001 - 留档失败不阻断恢复
                        logger.warning(
                            "[slave_market] 恢复前留档失败（继续恢复）：%s", e
                        )
                self._close()
                try:
                    # 恢复路径使用 sqlite3.Connection.backup() API 把备份文件流式写到
                    # 主库：backup() 内部会主动 truncate 目标并接管句柄，不会像
                    # copyfile 那样受源/目标 inode 的 mmap 影响。Windows 上即便前一个
                    # 短连接还残留 mmap 也能可靠覆盖。先 checkpoint 让主库的 WAL
                    # 状态对齐到 main file，避免"恢复后马上再写又把 WAL 合并回去"造成
                    # 状态混乱。
                    chk = self._connect()
                    chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    chk.close()
                    src = sqlite3.connect(src_path, timeout=15)
                    try:
                        dst = sqlite3.connect(self.path, timeout=15)
                        try:
                            src.backup(dst)
                        finally:
                            dst.close()
                    finally:
                        src.close()
                except OSError as e:
                    logger.error("[slave_market] 恢复备份失败 %s: %s", name, e)
                    raise
                # 立刻重连并补齐结构，避免下一条指令拿到半初始化的库
                conn = self._connect()
                conn.executescript(_SCHEMA)
                self._migrate(conn)
                conn.commit()
        return name

    def _list_backups_sync(self) -> list[str]:
        """常规备份列表。prerestore 保命快照在子目录里，不参与列表与序号。"""
        if not self._backup_root.is_dir():
            return []
        return sorted(
            (p.name for p in self._backup_root.glob(f"{_BACKUP_PREFIX}*.db")),
            reverse=True,
        )

    async def delete_backup(self, index: int) -> str | None:
        return await asyncio.to_thread(self._delete_backup_sync, int(index))

    def _delete_backup_sync(self, index: int) -> str | None:
        with self._lock:  # 与恢复同理：下标解析必须和删除在同一临界区
            with self._inflight_guard():
                backups = self._list_backups_sync()
                if index < 1 or index > len(backups):
                    return None
                name = backups[index - 1]
                try:
                    (self._backup_root / name).unlink(missing_ok=True)
                except OSError as e:
                    logger.error("[slave_market] 删除备份失败 %s: %s", name, e)
                    raise
        return name

    # ---------- 关闭 ----------

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        # 必须持锁：另一个 to_thread 线程可能正在写入，裸 close 会让它拿到
        # "Cannot operate on a closed database" 并丢掉这次写。
        # 另要给在跑的 worker 一点时间退出：to_thread 把任务派给默认
        # ThreadPoolExecutor，关 pool 也只能等 worker 主动结束。我们用
        # _inflight_cv 等一会儿，再 _close 并标记 _closed=True；新 worker
        # 进入 _connect() 会看到 _closed=True 直接抛 ProgrammingError。
        import time as _t
        deadline = _t.monotonic() + 5.0  # 给正在跑的事务最多 5s 自然退出
        with self._lock:
            while self._inflight > 0 and _t.monotonic() < deadline:
                self._inflight_cv.wait(timeout=0.1)
            self._close()
            self._closed = True
