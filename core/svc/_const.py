"""游戏数值常量与纯函数工具（拆分自原 core/service.py 顶部）。

配置默认值 / min / max 的**唯一事实来源**是 `_conf_schema.json`：
本模块不再各自硬编码默认值，改配置只需动 schema 一处。
读取失败（如以 .pyc 形式发布、路径不可达）时回退空表，`_num`/`_int` 再用
调用方兜底参数，绝不让游戏逻辑因缺 schema 崩溃。
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

# 一键升级信用的单次上限：防止配置成 upgradePriceMulti<=1 时同步空转卡死事件循环
MAX_AUTO_UPGRADES = 100
# 列表/排行榜单次返回上限
MARKET_LIMIT = 100
BOARD_LIMIT = 15
# 全量扫描型榜单（slave/bank）单次最多读多少行：超出只能近似截断，
# 防止异常膨胀的库把整表读进内存
FULL_SCAN_CAP = 10000

# ---------------------------------------------------------------------------
# 配置默认值 / min / max 的唯一事实来源：_conf_schema.json。
# service.py 不再各自硬编码默认值，改配置只需动 schema 一处。
# 读取失败（如以 .pyc 形式发布、路径不可达）时回退空表，_num/_int 再用
# 调用方兜底参数，绝不让游戏逻辑因缺 schema 崩溃。
# ---------------------------------------------------------------------------
_SCHEMA_CACHE: dict[str, dict] | None = None

# _conf_schema.json 的查找路径：从本文件向上逐级找，兼容 core/service.py（单体）
# 与 core/svc/_const.py（拆分后）两种布局——写死 parent.parent 在拆分后会指向
# core/ 而不是插件根目录，schema 读不到会让所有 _num/_int 静默回落到 0。
_SCHEMA_CANDIDATES = [
    p / "_conf_schema.json" for p in Path(__file__).resolve().parents
]


def _read_schema() -> dict:
    """读 _conf_schema.json（找不到/损坏返回空表，由调用方兜底）。"""
    for cand in _SCHEMA_CANDIDATES:
        try:
            if cand.is_file():
                data = json.loads(cand.read_text("utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


def _schema_meta() -> dict[str, dict]:
    """扁平化 _conf_schema.json -> {"work.slaveownerCooldown": {"default","min","max"}}。"""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    out: dict[str, dict] = {}
    schema = _read_schema()
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("type") == "object":
            for sk, smeta in (meta.get("items") or {}).items():
                if isinstance(smeta, dict):
                    out[f"{key}.{sk}"] = {
                        "default": smeta.get("default"),
                        "min": smeta.get("min"),
                        "max": smeta.get("max"),
                    }
        else:
            out[key] = {
                "default": meta.get("default"),
                "min": meta.get("min"),
                "max": meta.get("max"),
            }
    _SCHEMA_CACHE = out
    return out


def _fmt(x: float) -> str:
    return f"{float(x):.2f}"


def _cd_text(seconds: int) -> str:
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _sample(lst: list) -> str:
    return random.choice(lst)


def _now() -> int:
    return int(datetime.now().timestamp())


def _iso_week(ts: int) -> tuple[int, int]:
    """时间戳 -> (ISO 年, ISO 周)。跨年时第 52/53 周与第 1 周也能正确区分。

    坏时间戳（毫秒级、超范围）不能让指令直接报错，回退成 (0, 0)。
    """
    try:
        c = datetime.fromtimestamp(int(ts)).isocalendar()
    except (ValueError, OSError, OverflowError, TypeError):
        return (0, 0)
    return (c[0], c[1])
