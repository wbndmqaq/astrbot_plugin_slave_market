"""备份域（拆分自原 core/service.py）。"""

from __future__ import annotations

from ..result import notice


class _BackupMixin:

    async def create_backup(self) -> dict:
        name = await self.db.create_backup()
        return notice(
            "💾",
            "备份创建成功！",
            [f"备份文件：{name}", "可使用「奴隶备份列表」查看所有备份"],
        )

    async def list_backups(self) -> dict:
        backups = await self.db.list_backups()
        if not backups:
            return notice("📭", "当前没有任何备份", [], tone="warn")
        return notice(
            "🗄️",
            "备份列表",
            [f"{i + 1}. {b}" for i, b in enumerate(backups)]
            + ["可使用「奴隶恢复备份 序号」恢复"],
        )

    async def restore_backup(self, index: int) -> dict:
        try:
            name = await self.db.restore_backup(index)
        except ValueError as e:  # 备份文件损坏，已在 db 层拒绝覆盖主库
            return notice("🚫", "恢复失败", [str(e)], tone="err")
        if name is None:
            return notice("🚫", "无效的备份序号", [], tone="err")
        return notice("✅", "备份恢复成功！", [f"恢复时间点：{name}"])

    async def delete_backup(self, index: int) -> dict:
        name = await self.db.delete_backup(index)
        if name is None:
            return notice("🚫", "无效的备份序号", [], tone="err")
        return notice("🗑️", "备份删除成功！", [f"已删除：{name}"])

    # ================= WebUI 数据接口 =================
