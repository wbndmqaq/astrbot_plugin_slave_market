"""WebUI 数据接口域（拆分自原 core/service.py）。"""

from __future__ import annotations


class _WebApiMixin:

    async def stats(self) -> dict:
        """全局统计（WebUI 总览用）：单条聚合 SQL 直接出结果。"""
        return await self.db.totals()

    async def group_counts(self) -> list[dict]:
        return await self.db.group_counts()

    def profile_of(self, gid: str, uid: str, p: dict) -> dict:
        return {
            "gid": str(gid),
            "uid": str(uid),
            "nickname": p.get("nickname") or f"用户{uid}",
            "currency": round(p["currency"], 2),
            "value": round(p["value"], 2),
            "master": p.get("master") or "",
            "slave_count": len(p["slave"]),
            "bank_level": p["bank"]["level"],
            "bank_balance": round(p["bank"]["balance"], 2),
            "bank_limit": p["bank"]["limit"],
            "tier": p["ranking"]["tier"],
            "rank_score": p["ranking"]["score"],
            "wins": p["battleStats"]["wins"],
            "losses": p["battleStats"]["losses"],
        }

    async def page_profiles(
        self, gid: str, page: int = 1, size: int = 20
    ) -> tuple[int, list[dict]]:
        """分页取玩家档案 -> (总数, 本页档案)。WebUI 玩家列表用。"""
        page = max(1, int(page))
        size = min(200, max(1, int(size)))
        total = await self.db.count_players(gid)
        rows = await self.db.query_players(
            gid, order_by="uid", limit=size, offset=(page - 1) * size
        )
        return total, [self.profile_of(gid, uid, p) for uid, p in rows]

    async def all_profiles(self, gid: str, limit: int | None = None) -> list[dict]:
        """取某群全部（或前 limit 个）玩家档案。

        limit 必须有界：WebUI 的 /api/players_all 会跨群调用它，
        无 LIMIT 的 `SELECT *` 会把整群读进内存。
        """
        rows = await self.db.query_players(gid, order_by="uid", limit=limit)
        return [self.profile_of(gid, uid, p) for uid, p in rows]

    async def search_profiles(
        self, gid: str, kw: str, limit: int = 20
    ) -> list[dict]:
        """按 uid / 昵称搜索玩家档案（kw 下推到 SQL + LIMIT）。"""
        rows = await self.db.search_players(gid, kw, limit)
        return [self.profile_of(gid, uid, p) for uid, p in rows]

    async def all_player_refs_capped(self, cap: int) -> tuple[list[dict], bool]:
        """跨群取 (gid, uid, nickname) 与「是否被上限截断」——单条 SQL，无 N+1。

        多取一行来判定截断：恰好装满 cap 行并不等于有内容被丢弃，用
        `len(rows) >= cap` 判定会在玩家数正好等于 cap 时误报（前端会弹一条
        "玩家过多，仅加载前 N 人"的提示，而实际一个都没少）。
        """
        cap = max(1, int(cap))
        rows = await self.db.all_player_refs(cap + 1)
        truncated = len(rows) > cap
        refs = [
            {
                "gid": r["gid"],
                "uid": r["uid"],
                "nickname": r["nickname"] or f"用户{r['uid']}",
            }
            for r in rows[:cap]
        ]
        return refs, truncated
