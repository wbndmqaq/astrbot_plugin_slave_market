"""玩家级读写与删除留档（拆分自原 core/db.py 的"玩家级操作"段）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from astrbot.api import logger

from ._const import _NICK_MAX, _TRASH_KEEP, _row_to_player


class _PlayersMixin:
    # ---------- 玩家级操作 ----------

    def _read_row(
        self, conn: sqlite3.Connection, gid: str, uid: str
    ) -> tuple[dict, bool]:
        """读单行 -> (玩家数据, 是否已有存档行)。解析失败时先留档再降级为新号。

        已取证过的坏行（broken=1）直接返回空号：避免每次读都往 trash 里再插一份，
        把 `_TRASH_KEEP` 上限冲掉。

        **本方法绝不 commit**：它既被 `_load_sync`（只读路径）调用，也被事务内的
        `Txn.get` 调用。在事务中途 commit 会把「读一行坏数据」变成一次提交点，
        于是一旦之后 `tx.abort()` 回滚，之前已经写入的其他玩家改动无法撤销
        （部分写入被提交，整段读改写不再是原子）；同时每遇到一个坏行都会多一次
        commit。留档与 broken=1 标记都交给调用方事务的末尾统一提交，
        Abort 回滚时留档一起丢弃是可接受的（下次读会重新取证一次）。
        """
        row = conn.execute(
            "SELECT * FROM players WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        if row is None:
            return self.new_player(), False
        # 取证过一次后立刻打标记：下一次 _read_row 直接跳过归档，
        # 避免 _TRASH_KEEP 上限被同一行坏数据冲掉。
        # SIM118 抑制理由：sqlite3.Row 的 `in` / 迭代走的是**列值**而不是列名
        # （实测 `'broken' in row` 恒为 False），必须显式用 keys()。
        if "broken" in row.keys() and row["broken"]:  # noqa: SIM118
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
                pass  # 标记失败（库只读等）不影响返回新号，降级为下一轮再试
            # 已留档到 trash，可人工恢复；返回新号让指令继续走完而不是整条指令报错
            return self.new_player(), True

    def _archive_row(
        self, conn: sqlite3.Connection, gid: str, uid: str, row, reason: str = ""
    ) -> None:
        """把整行原样存进 trash（不删原行），用于删除留档与坏行取证。"""
        # 必须显式 keys()：sqlite3.Row 迭代产出的是列值，`{k: row[k] for k in row}`
        # 会直接抛 IndexError（下标必须是 int 或列名）
        payload = {k: row[k] for k in row.keys()}  # noqa: SIM118
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

    async def exists(self, group_id: str, user_id: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, str(group_id), str(user_id))

    def _exists_sync(self, gid: str, uid: str) -> bool:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "SELECT 1 FROM players WHERE gid=? AND uid=?", (gid, uid)
                    )
                    return cur.fetchone() is not None
                finally:
                    conn.close()

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

    async def delete(self, group_id: str, user_id: str) -> None:
        """删除存档：整行挪入 trash 留档，并清理其他玩家对该 uid 的主/奴引用。"""
        await asyncio.to_thread(self._delete_sync, str(group_id), str(user_id))

    def _delete_sync(self, gid: str, uid: str) -> None:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT * FROM players WHERE gid=? AND uid=?", (gid, uid)
                    ).fetchone()
                    if row is None:
                        return
                    try:
                        self._archive_row(conn, gid, uid, row, reason="deleted")
                        conn.execute(
                            "DELETE FROM players WHERE gid=? AND uid=?", (gid, uid)
                        )
                        self._unlink_refs(conn, gid, uid)
                        conn.commit()
                    except Exception:
                        conn.rollback()  # 防止部分语句残留到下一事务
                        raise
                finally:
                    conn.close()

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

    async def set_card(self, group_id: str, user_id: str, card: str) -> None:
        """登记平台昵称。只更新已有存档，**不为未参与游戏的人建档**。"""
        await asyncio.to_thread(self._set_card_sync, str(group_id), str(user_id), card)

    def _set_card_sync(self, gid: str, uid: str, card: str) -> None:
        with self._lock:
            with self._inflight_guard():
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE players SET nickname=?, updated_at=? WHERE gid=? AND uid=?",
                        (str(card or "")[:_NICK_MAX], int(time.time()), gid, uid),
                    )
                    conn.commit()
                finally:
                    conn.close()

