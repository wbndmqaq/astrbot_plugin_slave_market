"""备份域（拆分自原 core/service.py）。"""

from __future__ import annotations

from ..result import notice


class _BackupMixin:

    async def create_backup(self) -> dict:
        name = await self.db.create_backup()
        return notice(
            "💾",
            self.t("ui_backup_created", "备份创建成功！"),
            [
                self.t("ui_backup_file", "备份文件：{name}", name=name),
                self.t("ui_backup_hint_list", "可使用「奴隶备份列表」查看所有备份"),
            ],
        )

    async def list_backups(self) -> dict:
        backups = await self.db.list_backups()
        if not backups:
            return notice(
                "📭", self.t("ui_backup_none", "当前没有任何备份"), [], tone="warn"
            )
        return notice(
            "🗄️",
            self.t("ui_backup_list_title", "备份列表"),
            [f"{i + 1}. {b}" for i, b in enumerate(backups)]
            + [self.t("ui_backup_hint_restore", "可使用「奴隶恢复备份 序号」恢复")],
        )

    async def restore_backup(self, index: int) -> dict:
        try:
            name = await self.db.restore_backup(index)
        except ValueError as e:  # 备份文件损坏，已在 db 层拒绝覆盖主库
            return notice(
                "🚫", self.t("ui_backup_restore_fail", "恢复失败"), [str(e)], tone="err"
            )
        if name is None:
            return notice(
                "🚫", self.t("ui_backup_bad_index", "无效的备份序号"), [], tone="err"
            )
        return notice(
            "✅",
            self.t("ui_backup_restored", "备份恢复成功！"),
            [self.t("ui_backup_restored_to", "恢复时间点：{name}", name=name)],
        )

    async def delete_backup(self, index: int) -> dict:
        name = await self.db.delete_backup(index)
        if name is None:
            return notice(
                "🚫", self.t("ui_backup_bad_index", "无效的备份序号"), [], tone="err"
            )
        return notice(
            "🗑️",
            self.t("ui_backup_deleted", "备份删除成功！"),
            [self.t("ui_backup_deleted_name", "已删除：{name}", name=name)],
        )

    # ================= WebUI 数据接口 =================
