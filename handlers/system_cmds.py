"""系统域指令：帮助 / 数据备份（管理员）。"""

try:  # 兼容包加载与 sys.path 加载两种方式
    from ..core.result import R, notice
except ImportError:  # pragma: no cover
    from core.result import R, notice  # type: ignore

from .base import Route, numbers

P = r"^[！!]"


async def slave_help(ctx, event):
    """查看奴隶市场游戏帮助。"""
    data = ctx.texts.help
    return R(
        tmpl="help",
        data={
            "title": data.get("title"),
            "sub": data.get("sub"),
            "sections": data.get("sections", []),
        },
        text=str(data.get("text") or "奴隶市场帮助"),
    )


def _backup_route(kind: str):
    async def _run(ctx, event):
        if kind in ("restore", "delete"):
            nums = numbers(event)
            if not nums:
                return notice(
                    "🚫",
                    f"用法：奴隶{'恢复' if kind == 'restore' else '删除'}备份 序号",
                    [],
                    tone="warn",
                )
            index = int(nums[0])
        else:
            index = 0
        fn = {
            "create": ctx.service.create_backup,
            "list": ctx.service.list_backups,
            "restore": ctx.service.restore_backup,
            "delete": ctx.service.delete_backup,
        }[kind]
        return await fn(index) if kind in ("restore", "delete") else await fn()

    return _run


ROUTES = [
    Route(
        P + r"(奴隶|群友|nl)(帮助|菜单)$",
        "cmd_help",
        "查看奴隶市场游戏帮助",
        slave_help,
        group_only=False,
    ),
    Route(
        P + r"奴隶备份列表$",
        "cmd_backup_list",
        "查看所有游戏数据备份（管理员）",
        _backup_route("list"),
        admin=True,
        group_only=False,
    ),
    Route(
        P + r"奴隶恢复备份",
        "cmd_backup_restore",
        "恢复指定序号的备份（管理员）",
        _backup_route("restore"),
        admin=True,
        group_only=False,
    ),
    Route(
        P + r"奴隶删除备份",
        "cmd_backup_delete",
        "删除指定序号的备份（管理员）",
        _backup_route("delete"),
        admin=True,
        group_only=False,
    ),
    Route(
        P + r"(奴隶|群友|nl)备份$",
        "cmd_backup_create",
        "创建游戏数据全量备份（管理员）",
        _backup_route("create"),
        admin=True,
        group_only=False,
    ),
]
