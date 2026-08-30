"""战斗域指令：训练 / 一键训练 / 决斗 / 排位赛。

群聊限定校验由 base.install() 统一处理（Route.group_only 默认 True）。
"""

try:  # 兼容包加载与 sys.path 加载两种方式
    from ..core.result import notice
except ImportError:  # pragma: no cover
    from core.result import notice  # type: ignore

from .base import Route, at_targets, gid_of, nickname_of, numbers, target_of, uid_of

P = r"^[！!]"


async def train(ctx, event):
    """训练指定奴隶，提升其身价。"""
    target = target_of(event)
    if not target:
        return notice("🚫", "请 @ 要训练的奴隶，或输入对方 ID", [], tone="warn")
    return await ctx.service.train(
        gid_of(event), uid_of(event), nickname_of(event), target
    )


async def train_all(ctx, event):
    """训练自己的所有奴隶。"""
    return await ctx.service.train_all(gid_of(event), uid_of(event), nickname_of(event))


async def arena(ctx, event):
    """让两个奴隶决斗：决斗 @奴隶1 @奴隶2（两者都须为你所有）。"""
    self_id = str(event.get_self_id())
    ids = at_targets(event) or [n for n in numbers(event) if n != self_id]
    if len(ids) < 2:
        return notice("🚫", "用法：决斗 @奴隶1 @奴隶2", [], tone="warn")
    return await ctx.service.arena(
        gid_of(event), uid_of(event), nickname_of(event), ids[0], ids[1]
    )


async def ranking_show(ctx, event):
    """查看自己奴隶的排位赛信息。"""
    return await ctx.service.ranking_show(
        gid_of(event), uid_of(event), nickname_of(event)
    )


async def ranking_join(ctx, event):
    """让指定奴隶参加排位赛，赢取金币与分数。"""
    target = target_of(event)
    if not target:
        return notice("🚫", "请 @ 参赛的奴隶，或输入对方 ID", [], tone="warn")
    return await ctx.service.ranking_join(
        gid_of(event), uid_of(event), nickname_of(event), target
    )


ROUTES = [
    Route(P + r"一键训练$", "cmd_train_all", "训练自己的所有奴隶", train_all),
    Route(P + r"训练([\s@\d]|$)", "cmd_train", "训练指定奴隶，提升其身价", train),
    Route(P + r"决斗([\s@\d]|$)", "cmd_arena", "让两个奴隶决斗", arena),
    Route(
        P + r"参加排位赛([\s@\d]|$)",
        "cmd_ranking_join",
        "让指定奴隶参加排位赛",
        ranking_join,
    ),
    Route(P + r"排位赛$", "cmd_ranking_show", "查看奴隶排位赛信息", ranking_show),
]
