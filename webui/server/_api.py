"""公开元信息、只读查询 API 与玩家/备份管理 API（拆分自原 webui/server.py）。"""

from __future__ import annotations

from ._const import (
    PAGE_SIZE,
    PLAYERS_ALL_CAP,
    SEARCH_LIMIT,
    _finite,
    _index_arg,
    _json,
    _json_threaded,
)


class _NotFound(Exception):
    """目标档案在事务内已不存在（存在性校验必须放进事务，见 _admin_save）。"""


class _ApiMixin:
    # ===== 公开 =====

    async def _meta(self, request):
        """公开元信息。**不含 port/now 等指纹**：本端点未鉴权可达。

        version 属于插件市场展示信息，保留；端口号与服务器当前时间没有前端
        消费者，暴露它们只会给扫描器提供"这里有个管理面板"的线索。
        """
        return _json(
            {
                "name": "astrbot_plugin_slave_market",
                "display": "奴隶市场",
                "version": self.version,
                "auth_required": self.auth_on,
                "page_size": PAGE_SIZE,
            }
        )

    # ===== 鉴权端点（鉴权由 _guard 中间件统一完成） =====

    async def _overview(self, request):
        return _json({"stats": await self.ctx.service.stats()})

    async def _groups(self, request):
        return _json({"groups": await self.ctx.service.group_counts()})

    async def _ranking(self, request):
        gid = request.query.get("gid", "")
        kind = request.query.get("kind", "currency")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        kind = kind if kind in ("currency", "value", "slave", "bank") else "currency"
        r = await self.ctx.service.leaderboard(gid, kind)
        return await _json_threaded(
            {"kind": kind, "rows": r.get("data", {}).get("rows", [])}
        )

    async def _market(self, request):
        gid = request.query.get("gid", "")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        r = await self.ctx.service.market_list(gid)
        return await _json_threaded({"items": r.get("data", {}).get("items", [])})

    async def _players(self, request):
        gid = request.query.get("gid", "")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        try:
            page = max(1, int(request.query.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        try:
            size = min(200, max(1, int(request.query.get("size", str(PAGE_SIZE)))))
        except (TypeError, ValueError):
            size = PAGE_SIZE
        # 分页下推到 SQL，避免全量读取再切片
        total, players = await self.ctx.service.page_profiles(gid, page, size)
        return await _json_threaded(
            {"total": total, "page": page, "size": size, "players": players}
        )

    async def _search(self, request):
        """按 uid / 昵称搜索：kw 下推到 SQL，只回前 SEARCH_LIMIT 行。

        旧实现是 `all_profiles(gid)` 全量取回再在 Python 里 filter——每次点
        搜索都是 O(全群行数)，大群下点一次卡一次。
        """
        gid = request.query.get("gid", "")
        kw = request.query.get("kw", "").strip()[:64]
        if not gid or not kw:
            return _json({"results": []})
        results = await self.ctx.service.search_profiles(gid, kw, SEARCH_LIMIT)
        return await _json_threaded({"results": results})

    async def _players_all(self, request):
        """全部玩家（跨群，供免冷却选择器等使用）。

        有硬上限 PLAYERS_ALL_CAP，且走单条跨群 SQL（旧实现是「每群一次
        all_profiles」的 N+1，每行还构造完整档案再丢掉大部分字段）。
        `truncated` 只在真的丢了人才为真 —— 由服务层多取一行判定。
        """
        players, truncated = await self.ctx.service.all_player_refs_capped(
            PLAYERS_ALL_CAP
        )
        return await _json_threaded(
            {"players": players, "cap": PLAYERS_ALL_CAP, "truncated": truncated}
        )

    # ===== 玩家管理 =====

    async def _admin_get(self, request):
        gid = request.query.get("gid", "")
        uid = request.query.get("uid", "")
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)
        if not await self.ctx.service.db.exists(gid, uid):
            return _json({"error": "未找到"}, 404)
        data = await self.ctx.service.db.load(gid, uid)
        return _json({"profile": self.ctx.service.profile_of(gid, uid, data)})

    async def _admin_save(self, request):
        body, err = await self._body(request)
        if err:
            return err
        gid = str(body.get("gid", ""))
        uid = str(body.get("uid", ""))
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)

        db = self.ctx.service.db
        rejected: list[str] = []

        def _apply(tx):
            # 存在性校验必须放进事务内：exists()→transact 两步之间目标可能被
            # 删档，事务外校验通过后 tx.get(uid) 仍会按新号模板建档，把刚被
            # 删除的档案用面板表单值复活成幽灵行。
            if not tx.exists(uid):
                raise _NotFound
            data = tx.get(uid)
            for key, (path, caster, lo) in {
                "currency": (("currency",), float, 0.0),
                "value": (("value",), float, 0.0),
                "bank_level": (("bank", "level"), int, 1),
                "bank_balance": (("bank", "balance"), float, 0.0),
            }.items():
                if key not in body:
                    continue
                v = _finite(body[key])
                if v is None:
                    rejected.append(key)
                    continue
                v = caster(max(lo, v))
                if len(path) == 1:
                    data[path[0]] = v
                else:
                    data[path[0]][path[1]] = v
            # 主人变更必须双向维护：只改自己的 master 会留下"我认他为主、
            # 他名下却没有我"的单向关系，赎身/出售等逻辑会算错
            if "master" in body:
                new_master = str(body.get("master") or "").strip()
                old_master = str(data.get("master") or "")
                if new_master != old_master:
                    # 先校验新值再动数据：否则填错一个 ID 就把原本合法的
                    # 主奴关系解绑掉，奴隶白白变成自由人
                    if new_master and new_master == uid:
                        rejected.append("master（不能是自己）")
                    elif new_master and not tx.exists(new_master):
                        rejected.append("master（该玩家不存在）")
                    else:
                        if old_master and tx.exists(old_master):
                            self.ctx.service._drop_slave(tx.get(old_master), uid)
                        if new_master:
                            self.ctx.service._add_slave(tx.get(new_master), uid)
                        data["master"] = new_master
            return data

        try:
            data = await db.transact(gid, _apply)
        except _NotFound:
            return _json({"error": "未找到"}, 404)
        return _json(
            {
                "ok": True,
                "rejected": rejected,
                "profile": self.ctx.service.profile_of(gid, uid, data),
            }
        )

    async def _admin_delete(self, request):
        body, err = await self._body(request)
        if err:
            return err
        gid = str(body.get("gid", ""))
        uid = str(body.get("uid", ""))
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)
        if not await self.ctx.service.db.exists(gid, uid):
            return _json({"error": "未找到"}, 404)
        # db.delete 会同时清理其他玩家对该 uid 的 master/slave 引用
        await self.ctx.service.db.delete(gid, uid)
        return _json({"ok": True})

    # ===== 备份 =====

    async def _backup_list(self, request):
        backups = await self.ctx.service.db.list_backups()
        return _json(
            {"backups": [{"index": i + 1, "name": b} for i, b in enumerate(backups)]}
        )

    async def _backup_create(self, request):
        return _json({"ok": True, "name": await self.ctx.service.db.create_backup()})

    async def _backup_restore(self, request):
        body, err = await self._body(request)
        if err:
            return err
        index = _index_arg(body)
        if index is None:
            return _json({"error": "序号非法"}, 400)
        try:
            name = await self.ctx.service.db.restore_backup(index)
        except ValueError as e:  # 备份损坏，db 层已拒绝覆盖主库
            return _json({"error": str(e)}, 400)
        if not name:
            return _json({"error": "未找到"}, 404)
        return _json({"ok": True, "restored": name})

    async def _backup_delete(self, request):
        body, err = await self._body(request)
        if err:
            return err
        index = _index_arg(body)
        if index is None:
            return _json({"error": "序号非法"}, 400)
        name = await self.ctx.service.db.delete_backup(index)
        if not name:
            return _json({"error": "未找到"}, 404)
        return _json({"ok": True, "deleted": name})
