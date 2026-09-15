"""查询与排行榜域（拆分自原 core/service.py）。"""

from __future__ import annotations

from typing import ClassVar

from ..db._const import _VALUE_DEFAULT
from ..result import R
from ._const import BOARD_LIMIT, FULL_SCAN_CAP, MARKET_LIMIT, _fmt


class _BoardMixin:

    async def my_slave(self, group_id: str, user_id: str, nickname: str) -> dict:
        data = await self.get_player(group_id, user_id, nickname)
        master_name = ""
        if data["master"]:
            master_name = await self.name_of(group_id, data["master"])
        # 与 ranking_show 同源改造：N+1 → 1 次 SELECT IN (..)
        slave_ids = [str(s) for s in data["slave"]]
        slaves_by_id = dict(
            await self.db.query_players_by_uids(group_id, slave_ids)
        )
        slaves = []
        for sid in slave_ids:
            slave = slaves_by_id.get(sid)
            if slave is None:
                continue
            slaves.append(
                {
                    "id": sid,
                    "name": self._name(slave, sid),
                    "value": _fmt(slave["value"]),
                }
            )
        info = {
            "nickname": data.get("nickname")
            or nickname
            or self.t("ui_unknown_user", "用户{uid}", uid=user_id),
            "uid": str(user_id),
            "currency": _fmt(data["currency"]),
            "value": _fmt(data["value"]),
            "slave_count": len(slaves),
            "master": master_name or self.t("ui_no_owner", "无"),
            "slaves": slaves[:MARKET_LIMIT],
            "slaves_truncated": len(slaves) > MARKET_LIMIT,
            "wins": data["battleStats"]["wins"],
            "losses": data["battleStats"]["losses"],
        }
        # 模板用的组合行（含数值）在服务层拼好，模板不写死文案与数字
        info["title"] = self.t(
            "tpl_myslave_title", "{name} 的奴籍账册", name=info["nickname"]
        )
        info["truncated_note"] = self.t(
            "tpl_myslave_truncated", "（奴隶过多，仅展示前 {limit}）", limit=MARKET_LIMIT
        )
        text = (
            self.t(
                "ui_myslave_head", "# {name} 的基础信息", name=info["nickname"]
            )
            + "\n"
            + self.t(
                "ui_myslave_money",
                "金币：{currency}｜身价：{value}",
                currency=info["currency"],
                value=info["value"],
            )
            + "\n"
            + self.t(
                "ui_myslave_slaves",
                "拥有奴隶：{count} 个｜主人：{master}",
                count=info["slave_count"],
                master=info["master"],
            )
            + "\n"
            + self.t(
                "ui_myslave_record",
                "决斗战绩：{wins} 胜 {losses} 负",
                wins=info["wins"],
                losses=info["losses"],
            )
        )
        if slaves:
            text += (
                "\n"
                + self.t("ui_myslave_list_head", "奴隶列表：")
                + "\n"
                + "\n".join(
                    self.t(
                        "ui_myslave_row",
                        "• {name}（{id}）身价 {value}",
                        name=s["name"],
                        id=s["id"],
                        value=s["value"],
                    )
                    for s in info["slaves"]
                )
            )
        return R(tmpl="myslave", data=info, text=text)

    async def market_list(self, group_id: str) -> dict:
        # 一条 SQL 按身价倒序取前 N
        rows = await self.db.query_players(
            group_id, order_by="value", desc=True, limit=MARKET_LIMIT
        )
        total = await self.db.count_players(group_id)
        names: dict[str, str] = {uid: self._name(p, uid) for uid, p in rows}
        # 主人昵称一次性批量补齐：避免主人在 top 100 之外时 market 列表里
        # 每条都 await self.name_of() 单次开/关 SQLite 连接，N+1 让指令慢 1~3s
        miss_ids = [
            mid
            for _, p in rows
            for mid in [p.get("master") or ""]
            if mid and mid not in names
        ]
        if miss_ids:
            extra = await self.db.query_players_by_uids(
                group_id, list(dict.fromkeys(miss_ids))
            )
            for uid, p in extra:
                names[uid] = self._name(p, uid)
        no_owner = self.t("ui_no_owner", "无")
        items = []
        for uid, p in rows:
            master_id = p.get("master") or ""
            if master_id:
                master_name = names.get(master_id) or no_owner
            else:
                master_name = no_owner
            items.append(
                {
                    "id": uid,
                    "name": names[uid],
                    "value": _fmt(p["value"]),
                    "master": master_name,
                }
            )
        text = (
            self.t(
                "ui_market_head", "🛒 奴隶市场（共 {total} 人）", total=total
            )
            + "\n"
            + "\n".join(
                self.t(
                    "ui_market_row",
                    "{rank}. {name}（{id}）身价 {value}｜主人：{master}",
                    rank=i,
                    name=it["name"],
                    id=it["id"],
                    value=it["value"],
                    master=it["master"],
                )
                for i, it in enumerate(items, 1)
            )
        )
        return R(
            tmpl="market",
            data={
                "items": items,
                "total": total,
                # 副标题含默认身价数值：在服务层用文案表拼好，模板不再写死数字
                "sub": self.t(
                    "tpl_market_sub",
                    "共 {total} 人在册 · 未出场过的身价均为 {value}",
                    total=total,
                    value=int(_VALUE_DEFAULT),
                ),
            },
            text=text,
        )

    # 排行榜类型 -> (排序列, 展示标题)
    # 榜单标题默认值保留在这里；uiTexts 里可用键 ui_board_title_<kind> 覆盖
    _BOARDS: ClassVar[dict[str, tuple[str, str]]] = {
        "currency": ("currency", "金币排行榜"),
        "value": ("value", "身价排行榜"),
        # slave 榜展示键是 JSON 数组长度、bank 榜是 (level, balance) 二元组，
        # 单一 SQL 列都无法正确预截断，只能全量取回后内存排序
        "slave": ("uid", "奴隶数量排行榜"),
        "bank": ("bank_level", "银行等级排行榜"),
    }

    async def leaderboard(self, group_id: str, kind: str) -> dict:
        """kind: currency / value / slave / bank"""
        if kind not in self._BOARDS:
            kind = "currency"
        col, title_default = self._BOARDS[kind]
        title = self.t("ui_board_title_" + kind, title_default)
        if kind in ("slave", "bank"):
            # 全量扫描：按 uid/level 预截断会采到错误样本——uid 倒序前 N 与
            # "奴隶最多"无关，level 前 15 会挤掉同级但余额更高的玩家。
            # 行数用 FULL_SCAN_CAP 兜底异常膨胀的库。
            rows = await self.db.query_players(
                group_id, order_by=col, desc=True, limit=FULL_SCAN_CAP
            )
        else:
            # currency/value 的展示键就是排序列本身：SQL 直接取前 15 名即可，
            # 并列名次谁进榜都一样，无需全量扫描
            rows = await self.db.query_players(
                group_id, order_by=col, desc=True, limit=BOARD_LIMIT
            )
        entries = [
            {
                "id": uid,
                "name": self._name(p, uid),
                "currency": p["currency"],
                "value": p["value"],
                "slave_count": len(p["slave"]),
                "bank_level": p["bank"]["level"],
                "bank_balance": p["bank"]["balance"],
            }
            for uid, p in rows
        ]
        keymap = {
            "currency": lambda e: e["currency"],
            "value": lambda e: e["value"],
            "slave": lambda e: e["slave_count"],
            "bank": lambda e: (e["bank_level"], e["bank_balance"]),
        }
        entries.sort(key=keymap[kind], reverse=True)
        entries = entries[:BOARD_LIMIT]

        def _score(e: dict) -> str:
            if kind == "currency":
                return f"{_fmt(e['currency'])} 💰"
            if kind == "value":
                return f"{_fmt(e['value'])} 💎"
            if kind == "slave":
                return self.t("ui_board_slave_count", "{count} 个", count=e["slave_count"])
            return self.t(
                "ui_board_bank_level",
                "Lv.{level}（{balance}）",
                level=e["bank_level"],
                balance=_fmt(e["bank_balance"]),
            )

        board = [
            {"rank": i + 1, "name": e["name"], "id": e["id"], "score": _score(e)}
            for i, e in enumerate(entries)
        ]
        text = (
            self.t(
                "ui_board_head",
                "🏆 {title}（前 {count} 名）",
                title=title,
                count=len(board),
            )
            + "\n"
            + "\n".join(
                self.t(
                    "ui_board_row",
                    "{rank}. {name}（{id}）- {score}",
                    rank=r["rank"],
                    name=r["name"],
                    id=r["id"],
                    score=r["score"],
                )
                for r in board
            )
        )
        return R(tmpl="ranking", data={"title": title, "rows": board}, text=text)

    # ================= 备份 =================
