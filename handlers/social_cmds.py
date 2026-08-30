"""个人与社交域指令：打工 / 抢劫 / 赎身。

群聊限定校验由 base.install() 统一处理（Route.group_only 默认 True）。
"""

try:  # 兼容包加载与 sys.path 加载两种方式
    from ..core.result import notice  # noqa: F401 - 供后续指令扩展
except ImportError:  # pragma: no cover
    from core.result import notice  # type: ignore # noqa: F401

from .base import Route, gid_of, is_admin, nickname_of, target_of, uid_of

P = r"^[！!]"


async def work(ctx, event):
    """打工赚钱：没有奴隶自己打工，有奴隶则让奴隶打工。"""
    return await ctx.service.work(
        gid_of(event), uid_of(event), nickname_of(event), is_admin(event)
    )


async def rob(ctx, event):
    """抢劫群友金币（可指定目标，未指定则随机；失败会被罚款）。"""
    return await ctx.service.rob(
        gid_of(event), uid_of(event), nickname_of(event), target_of(event)
    )


async def buyback(ctx, event):
    """以双倍身价买回自己的自由身。"""
    return await ctx.service.buyback(gid_of(event), uid_of(event), nickname_of(event))


ROUTES = [
    Route(P + r"(一键)?(打工|工作)$", "cmd_work", "打工赚钱", work),
    Route(P + r"(抢劫|打劫)([\s@\d]|$)", "cmd_rob", "抢劫群友金币", rob),
    Route(P + r"赎身$", "cmd_buyback", "以双倍身价买回自己的自由身", buyback),
]
