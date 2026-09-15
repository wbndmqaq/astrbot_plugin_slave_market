"""存储层常量、DDL 与行↔玩家数据的转换（拆分自原 core/db.py）。"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
import time
from pathlib import Path

_NICK_MAX = 64
# 回收站保留行数：与"备份文件份数"解耦，否则删档留档与坏行取证会互相挤掉
_TRASH_KEEP = 200
# 数值硬顶：SQLite INTEGER 是 64 位，超出会让 conn.execute 抛 OverflowError
_NUM_CAP = 2**62
# 备份文件名前缀；恢复前的保命快照另用前缀并放进子目录，不参与常规备份列表与裁剪
_BACKUP_PREFIX = "slave_market_"
_PRERESTORE_DIR = "prerestore"
_PRERESTORE_KEEP = 5

# schema 中「新号初始值」的键路径：本模块的常量 -> _conf_schema.json 的键。
# 能读到 schema 时以 schema 为唯一事实来源（与 core/service.py 的 _num/_int 一致），
# 读不到时保留下面这份 DDL 兜底常量。两边数值必须一致，改配置只需动 schema。
_BANK_DEFAULTS_SCHEMA_KEYS = (
    ("level", "initialLevel", 1),
    ("limit", "initialLimit", 1000),
    ("upgradePrice", "initialUpgradePrice", 100),
)


def _schema_candidates() -> list[Path]:
    """从本文件向上找 _conf_schema.json（兼容 core/db.py 与 core/db/_const.py 两种布局）。"""
    here = Path(__file__).resolve()
    return [p / "_conf_schema.json" for p in here.parents]


def _read_schema() -> dict:
    for cand in _schema_candidates():
        try:
            if cand.is_file():
                data = json.loads(cand.read_text("utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


# 段位初始分/名称的最终字面量兜底：内置正本 gameTexts.json 的 ranking_tiers
# 首档读不到（.pyc 发布 / resources 被裁剪 / 首档结构非法）时用它。
# 分数取「首档门槛 − 1」：svc._tier() 把门槛当【离开该档的上界】
# （score < threshold → 该档名，段位说明文案也是「青铜 <1000」），
# 初始分必须落在首档区间内，否则新号会「分数 1000 却挂着青铜」、
# 打第一场就被静默改成白银。
_RANK_FALLBACK: tuple[int, str] = (999, "青铜")
# 段位名会拼进 _SCHEMA 的 DDL 默认值（SQL 字面量）：超过该长度直接回落兜底
_RANK_NAME_MAX = 16


def _builtin_rank_default() -> tuple[int, str]:
    """段位初值：**内置正本** resources/data/gameTexts.json 的 ranking_tiers 首档。

    只读内置正本（只读文件），刻意不读用户覆盖：初始段位名会被拼进
    _SCHEMA 的 SQL 字面量，让用户可控文本进 DDL 是注入面。
    用户自定义的段位表走运行期 `PlayerDB.set_rank_init()`（由
    GameCtx.set_copywriting 传入），只影响新号模板。
    """
    try:
        from ..texts import builtin_path, read_json

        tiers = (read_json(builtin_path("gameTexts")) or {}).get("ranking_tiers")
    except ImportError:  # pragma: no cover - 只可能在极端裁剪的发布形态下发生
        return _RANK_FALLBACK
    first = tiers[0] if isinstance(tiers, list) and tiers else None
    if isinstance(first, (list, tuple)) and len(first) == 2:
        name = first[1]
        try:
            score = int(first[0])
        except (TypeError, ValueError):
            score = 0
        # 名字会被拼进 _SCHEMA 的 DDL 默认值，必须先净化再判定：
        # - 单/双引号：破坏 SQL 字面量引号配对（语法错误或注入面）；
        # - \x00：sqlite3.executescript 抛 "embedded null character"，
        #   会让 _init_sync() 上抛 -> main.initialize() 失败 -> 全部指令失效；
        # - 控制字符（含换行）：非法文本不该进 DDL；
        # - 超长（> _RANK_NAME_MAX）：不是合法段位名，禁止原样拼进 DDL。
        if isinstance(name, str):
            name = name.strip()
            if (
                score > 0
                and name
                and len(name) <= _RANK_NAME_MAX
                and not any(ch in name for ch in ("'", '"', "\x00"))
                and all(ch >= " " and ch != "\x7f" for ch in name)
            ):
                # 门槛是「离开该档」的上界（见 _RANK_FALLBACK 注释）：
                # 初始分取 threshold-1，保证新号确实落在首档区间里
                return max(0, score - 1), name
    return _RANK_FALLBACK


def _rank_init_from_tiers(tiers) -> tuple[int, str] | None:
    """段位表 -> 新号初始 (分数, 段位名)；首档结构非法时返回 None。

    运行期热更新路径（用户覆盖 gameTexts.json）经此推导「建号默认值」，
    取不到就保持 NEW_PLAYER 模板里的静态默认，绝不写入非法值。

    门槛语义与 svc._tier() 同源：门槛是【离开该档的上界】（score < threshold
    才算该档）。所以初始分取 threshold-1——用户把段位表改成 500 起步后，
    新号必须是 499/首档名，而不是 500（500 在 _tier() 里已经是下一档了，
    否则新号一建档就与段位说明自相矛盾，打一场后被静默改档）。
    """
    first = tiers[0] if isinstance(tiers, (list, tuple)) and tiers else None
    if not (isinstance(first, (list, tuple)) and len(first) == 2):
        return None
    name = first[1]
    try:
        score = int(first[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return max(0, score - 1), name


def _resolve_defaults() -> tuple[dict[str, int], int, str, float]:
    """返回 (银行初始值, 段位初始分, 段位初始名, 身价初值)。

    权威来源：_conf_schema.json 的 bank.initialLevel / initialLimit /
    initialUpgradePrice。段位初始分与名称取自 resources/data/gameTexts.json
    的 ranking_tiers 首档门槛-1（默认门槛 1000 → 新号 999 分 /「青铜」，
    保证初始分落在首档区间，见 _RANK_FALLBACK 注释），身价初值对应
    arena.minValue 的量纲基准 100。schema / gameTexts 拿不到（.pyc 发布 /
    resources 被裁剪）时回落到字面量兜底——它们与上述推导结果一致。
    """
    bank = {"level": 1, "limit": 1000, "upgradePrice": 100}
    schema = _read_schema()
    node = ((schema.get("bank") or {}).get("items")) or {}
    if isinstance(node, dict):
        for dst, src, _fallback in _BANK_DEFAULTS_SCHEMA_KEYS:
            try:
                v = int(node.get(src, {}).get("default"))
            except (TypeError, ValueError, AttributeError):
                continue
            if v > 0:
                bank[dst] = v
    score, tier = _builtin_rank_default()
    return bank, score, tier, 100.0


_BANK_DEFAULTS, _RANK_DEFAULT_SCORE, _RANK_DEFAULT_TIER, _VALUE_DEFAULT = (
    _resolve_defaults()
)

# 新玩家的初始数据模板（唯一模板，读取时必须经 new_player() 深拷贝）
NEW_PLAYER = {
    "currency": 0.0,
    "value": _VALUE_DEFAULT,
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
    "ranking": {"score": _RANK_DEFAULT_SCORE, "tier": _RANK_DEFAULT_TIER, "matches": 0},
    "bank": {
        "balance": 0.0,
        "level": _BANK_DEFAULTS["level"],
        "limit": _BANK_DEFAULTS["limit"],
        "upgradePrice": _BANK_DEFAULTS["upgradePrice"],
        "lastInterestTime": 0,
    },
}

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS players (
    gid                 TEXT    NOT NULL,
    uid                 TEXT    NOT NULL,
    nickname            TEXT    DEFAULT '',
    currency            REAL    DEFAULT 0,
    value               REAL    DEFAULT {_VALUE_DEFAULT},
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
    rank_score          INTEGER DEFAULT {_RANK_DEFAULT_SCORE},
    rank_tier           TEXT    DEFAULT '{_RANK_DEFAULT_TIER}',
    rank_matches        INTEGER DEFAULT 0,
    bank_balance        REAL    DEFAULT 0,
    bank_level          INTEGER DEFAULT {_BANK_DEFAULTS["level"]},
    bank_limit          INTEGER DEFAULT {_BANK_DEFAULTS["limit"]},
    bank_upgrade_price  INTEGER DEFAULT {_BANK_DEFAULTS["upgradePrice"]},
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


def _escape_like(kw: str) -> str:
    """转义 LIKE 的通配符，让用户输入的 % / _ 只是普通字符。

    不转义时搜 "%" 会匹配全表（等于退化成旧的全量接口），搜 "_" 同理。
    配合 SQL 里的 `ESCAPE '\\'` 使用。
    """
    s = str(kw)
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sanitize(data: dict) -> dict:
    """规范化玩家数据，修复缺失/非法字段。"""
    merged = copy.deepcopy(NEW_PLAYER)
    for k in NEW_PLAYER:
        if k in data:
            merged[k] = data[k]

    merged["currency"] = max(0.0, _to_float(merged.get("currency")))
    merged["value"] = max(0.0, _to_float(merged.get("value"), _VALUE_DEFAULT))

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
        "score": _to_int(ranking.get("score"), _RANK_DEFAULT_SCORE),
        "tier": str(ranking.get("tier") or _RANK_DEFAULT_TIER),
        "matches": max(0, _to_int(ranking.get("matches"))),
    }

    bank = merged.get("bank") or {}
    merged["bank"] = {
        "balance": max(0.0, _to_float(bank.get("balance"))),
        "level": max(1, _to_int(bank.get("level"), _BANK_DEFAULTS["level"])),
        "limit": max(1, _to_int(bank.get("limit"), _BANK_DEFAULTS["limit"])),
        "upgradePrice": max(
            1, _to_int(bank.get("upgradePrice"), _BANK_DEFAULTS["upgradePrice"])
        ),
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

# bad-row 留档标记 `broken`（见 _SCHEMA 的 players.broken）：一旦取证到 trash
# 就置 1，避免 _read_row 每次都往 trash 里再插一份

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


# S608 抑制理由：拼接进去的只有本模块常量列表 _COLS（列名）与 "?" 占位符个数，
# 所有取值一律走 ? 参数绑定。
_UPSERT = (
    f"INSERT OR REPLACE INTO players ({', '.join(_COLS)}, updated_at) "  # noqa: S608
    f"VALUES ({', '.join('?' for _ in range(len(_COLS) + 1))})"
)


