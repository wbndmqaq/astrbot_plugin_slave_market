"""市场域指令：购买 / 放生 / 我的奴隶 / 奴隶市场 / 排行榜。

群聊限定校验由 base.install() 统一处理（Route.group_only 默认 True）。
"""

try:  # 兼容包加载与 sys.path 加载两种方式
    from ..core.result import notice
except ImportError:  # pragma: no cover
    from core.result import notice  # type: ignore

from .base import Route, gid_of, nickname_of, target_of, uid_of

P = r"^[！!]"


async def purchase(ctx, event):
    """购买 @ 的群友为奴隶（价格为对方身价）。"""
    target = target_of(event)
    if not target:
        return notice("🚫", "请 @ 要购买的群友，或输入对方 ID", [], tone="warn")
    return await ctx.service.purchase(
        gid_of(event), uid_of(event), nickname_of(event), target
    )


async def release(ctx, event):
    """放生自己的奴隶。"""
    target = target_of(event)
    if not target:
        return notice("🚫", "请 @ 要放生的奴隶，或输入对方 ID", [], tone="warn")
    return await ctx.service.release(gid_of(event), uid_of(event), target)


async def my_slave(ctx, event):
    """查看自己的金币、身价与奴隶列表。"""
    return await ctx.service.my_slave(gid_of(event), uid_of(event), nickname_of(event))


async def market(ctx, event):
    """查看本群奴隶市场（所有玩家的身价与主人）。"""
    return await ctx.service.market_list(gid_of(event))


def _ranking_route(kind: str):
    async def _run(ctx, event):
        return await ctx.service.leaderboard(gid_of(event), kind)

    return _run


ROUTES = [
    Route(
        P + r"(购买群友|购买奴隶)([\s@\d]|$)",
        "cmd_purchase",
        "购买 @ 的群友为奴隶",
        purchase,
    ),
    Route(
        P + r"(放生群友|放生奴隶)([\s@\d]|$)", "cmd_release", "放生自己的奴隶", release
    ),
    Route(
        P + r"我的(群友|奴隶)$",
        "cmd_my_slave",
        "查看自己的金币、身价与奴隶列表",
        my_slave,
    ),
    Route(P + r"(群友市场|奴隶市场)$", "cmd_market", "查看本群奴隶市场", market),
    Route(
        P + r"奴隶身价排行榜$",
        "cmd_value_ranking",
        "查看本群身价排行榜",
        _ranking_route("value"),
    ),
    Route(
        P + r"奴隶资金排行榜$",
        "cmd_currency_ranking",
        "查看本群金币排行榜",
        _ranking_route("currency"),
    ),
    Route(
        P + r"(金币|资金)排行榜$",
        "cmd_currency_ranking2",
        "查看本群金币排行榜",
        _ranking_route("currency"),
    ),
    Route(
        P + r"身价排行榜$",
        "cmd_value_ranking2",
        "查看本群身价排行榜",
        _ranking_route("value"),
    ),
]
