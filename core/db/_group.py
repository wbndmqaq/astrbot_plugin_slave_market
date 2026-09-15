"""群级聚合查询（拆分自原 core/db.py 的"群级操作 / 聚合查询"段）。

聚合查询避免"先列 id 再逐个 load"的 N+1：一条 SQL 直接出结果或只取一页。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from ._const import _SORTABLE, _escape_like, _row_to_player, _to_float


class _GroupMixin:
    # ---------- 群级操作与聚合查询 ----------

    async def list_players(self, group_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_players_sync, str(group_id))

    def _list_players_sync(self, gid: str) -> list[str]:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT uid FROM players WHERE gid=? ORDER BY uid", (gid,)
                )
                return [r["uid"] for r in cur.fetchall()]
            finally:
                conn.close()

    async def group_counts(self) -> list[dict]:
        return await asyncio.to_thread(self._group_counts_sync)

    def _group_counts_sync(self) -> list[dict]:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT gid, COUNT(*) AS n FROM players GROUP BY gid ORDER BY gid"
                )
                return [
                    {"gid": r["gid"], "count": int(r["n"])} for r in cur.fetchall()
                ]
            finally:
                conn.close()

    # ---------- 聚合查询（避免"列 id 再逐个 load"的 N+1） ----------

    async def count_players(self, group_id: str) -> int:
        return await asyncio.to_thread(self._count_players_sync, str(group_id))

    def _count_players_sync(self, gid: str) -> int:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) AS n FROM players WHERE gid=?", (gid,)
                )
                return int(cur.fetchone()["n"])
            finally:
                conn.close()

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

    async def search_players(
        self, group_id: str, kw: str, limit: int = 20
    ) -> list[tuple[str, dict]]:
        """按 uid / 昵称模糊搜索（服务端过滤 + LIMIT），-> [(uid, 玩家数据)]。

        与 `query_players(limit=None)` 全量取回再在调用方过滤的差别：kw 下推到
        SQL，代价是 O(命中行数) 而不是 O(全群行数)。WebUI 的搜索框走这里。
        """
        return await asyncio.to_thread(
            self._search_players_sync, str(group_id), str(kw), int(limit)
        )

    def _search_players_sync(
        self, gid: str, kw: str, limit: int
    ) -> list[tuple[str, dict]]:
        if not kw:
            return []
        limit = max(1, min(200, int(limit)))
        like = f"%{_escape_like(kw)}%"
        # LIKE 的 % / _ 已由 _escape_like 转义，配合 ESCAPE '\' 生效
        sql = (
            "SELECT * FROM players WHERE gid=? AND "
            "(uid LIKE ? ESCAPE '\\' OR nickname LIKE ? ESCAPE '\\') "
            "ORDER BY uid ASC LIMIT ?"
        )
        out: list[tuple[str, dict]] = []
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                rows = conn.execute(sql, (gid, like, like, limit)).fetchall()
                for row in rows:
                    try:
                        out.append((row["uid"], _row_to_player(row)))
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "[slave_market] 跳过坏行 %s/%s: %s", gid, row["uid"], e
                        )
            finally:
                conn.close()
        return out

    async def all_player_refs(self, limit: int) -> list[dict]:
        """跨群取 (gid, uid, nickname) 列表，带硬上限。

        WebUI 的 /api/players_all 只需要这三个字段。旧实现是「先 group_counts，
        再对每个群调一次 all_profiles」——N+1 次线程池往返，而且每一行都构造了
        完整玩家档案（含 json.loads 奴隶列表）再丢掉 95% 的字段。
        limit 由调用方给（用它 +1 多取一行即可判断是否被截断）。
        """
        return await asyncio.to_thread(self._all_player_refs_sync, int(limit))

    def _all_player_refs_sync(self, limit: int) -> list[dict]:
        limit = max(1, int(limit))
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT gid, uid, nickname FROM players ORDER BY gid, uid LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        # 不在这里补「用户{uid}」这类展示文案：存储层不持有游戏文案（同 _KIND_* 约定），
        # 由服务层 profile 归一化时补。
        return [
            {"gid": str(r["gid"]), "uid": str(r["uid"]), "nickname": str(r["nickname"] or "")}
            for r in rows
        ]

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
        # 必须是 SELECT *：`_row_to_player(row)` 需要全部列。
        # 旧实现写成 `SELECT uid`，于是每一次取行都在 _row_to_player 里抛
        # 「No item with that key」被 except 吞掉 —— 该函数**永远返回空列表**，
        # 表现是「我的奴隶」永远显示 0 个奴隶、「排位赛」列表恒为空、
        # 市场里榜外主人的名字恒显示「无」。
        # placeholders 只是 "?,?,?" 个数，取值全部 ? 绑定
        sql = f"SELECT * FROM players WHERE gid=? AND uid IN ({placeholders})"  # noqa: S608
        params: list = [gid, *uniq]
        out: list[tuple[str, dict]] = []
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    try:
                        out.append((row["uid"], _row_to_player(row)))
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "[slave_market] 跳过坏行 %s/%s: %s", gid, row["uid"], e
                        )
            finally:
                conn.close()
        return out

    def _query_players_sync(
        self, gid: str, order_by: str, desc: bool, limit: int | None, offset: int
    ) -> list[tuple[str, dict]]:
        col = order_by if order_by in _SORTABLE else "uid"
        # col 已过白名单 _SORTABLE，排序方向是字面量，取值全部 ? 绑定
        sql = (
            f"SELECT * FROM players WHERE gid=? ORDER BY {col} "  # noqa: S608
            f"{'DESC' if desc else 'ASC'}, uid ASC"
        )
        params: list = [gid]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [max(0, int(limit)), max(0, int(offset))]
        out: list[tuple[str, dict]] = []
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    try:
                        out.append((row["uid"], _row_to_player(row)))
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "[slave_market] 跳过坏行 %s/%s: %s", gid, row["uid"], e
                        )
            finally:
                conn.close()
        return out

    async def totals(self) -> dict:
        """全局统计：单条聚合 SQL 直接出结果。"""
        return await asyncio.to_thread(self._totals_sync)

    def _totals_sync(self) -> dict:
        with self._lock, self._inflight_guard():
            conn = self._connect()
            try:
                r = conn.execute(
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
            finally:
                conn.close()

