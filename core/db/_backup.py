"""全量备份 / 恢复与生命周期收尾（拆分自原 core/db.py 的"全量备份 / 恢复"段）。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger

from ._const import (
    _BACKUP_PREFIX,
    _PRERESTORE_DIR,
    _PRERESTORE_KEEP,
    _SCHEMA,
)


class _BackupMixin:
    # ---------- 全量备份 / 恢复 ----------

    async def create_backup(self) -> str:
        return await asyncio.to_thread(self._create_backup_sync)

    def _create_backup_sync(self) -> str:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self._backup_root.mkdir(parents=True, exist_ok=True)
        with self._lock, self._inflight_guard():  # 命名全程持锁，杜绝并发撞名
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
                # 连接一律走 closing()：PRAGMA 抛错时也必须关掉，
                # 否则 Windows 上残留的文件句柄会让后续替换/删除主库失败。
                with contextlib.closing(self._connect()) as chk:
                    chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                with (
                    contextlib.closing(self._connect()) as src,
                    contextlib.closing(sqlite3.connect(dest, timeout=15)) as dst,
                ):
                    src.backup(dst)
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
        with self._lock, self._inflight_guard():
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
                    with (
                        contextlib.closing(self._connect()) as src_p,
                        contextlib.closing(sqlite3.connect(pre_path, timeout=15)) as dst_p,
                    ):
                        src_p.backup(dst_p)
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
                # closing() 兜住 PRAGMA 抛错路径：漏关的连接会在 Windows 上
                # 持有主库文件句柄，随后的 backup 写入会直接失败。
                with contextlib.closing(self._connect()) as chk:
                    chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                with contextlib.closing(
                    sqlite3.connect(src_path, timeout=15)
                ) as src, contextlib.closing(
                    sqlite3.connect(self.path, timeout=15)
                ) as dst:
                    src.backup(dst)
            except OSError as e:
                logger.error("[slave_market] 恢复备份失败 %s: %s", name, e)
                raise
            # 立刻重连并按 _SCHEMA 建表，避免下一条指令拿到半初始化的库。
            # 这里**不再补列**：恢复的快照与当前版本列集一致时不受任何影响；
            # 若恢复的是旧 schema 的快照，缺列不会被自动补上，首次读写会抛出
            # 带列名的清晰错误（需删除旧 db 让插件按 _SCHEMA 重建）。
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
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
        with self._lock, self._inflight_guard():  # 与恢复同理：下标解析必须和删除在同一临界区
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

