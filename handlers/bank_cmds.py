"""银行域指令：存取款 / 信用升级 / 利息 / 转账。

群聊限定校验由 base.install() 统一处理（Route.group_only 默认 True）。
"""

import re

from ..core.result import notice

from .base import MAX_ARG_LEN, Route, at_targets, gid_of, nickname_of, numbers, uid_of

P = r"^[！!]"


def _first_int(event) -> int | None:
    nums = numbers(event)
    return int(nums[0]) if nums else None


async def deposit(ctx, event):
    """存款到银行：存款 金额。"""
    amount = _first_int(event)
    if amount is None:
        return notice(
            "🚫", "用法：存款 金额（全部存入请用「一键存款」）", [], tone="warn"
        )
    return await ctx.service.bank_deposit(
        gid_of(event), uid_of(event), nickname_of(event), amount
    )


async def deposit_all(ctx, event):
    """把所有现金一键存入银行。"""
    return await ctx.service.bank_deposit(
        gid_of(event), uid_of(event), nickname_of(event), None
    )


async def withdraw(ctx, event):
    """从银行取款：取款 金额。"""
    amount = _first_int(event)
    if amount is None:
        return notice("🚫", "用法：取款 金额", [], tone="warn")
    return await ctx.service.bank_withdraw(
        gid_of(event), uid_of(event), nickname_of(event), amount
    )


async def bank_info(ctx, event):
    """查看银行信息与可领利息。"""
    return await ctx.service.bank_info(gid_of(event), uid_of(event), nickname_of(event))


async def collect_interest(ctx, event):
    """领取银行存款利息（每小时结算）。"""
    return await ctx.service.bank_interest(
        gid_of(event), uid_of(event), nickname_of(event)
    )


async def upgrade_credit(ctx, event):
    """升级信用等级，提升存款上限。"""
    return await ctx.service.bank_upgrade(
        gid_of(event), uid_of(event), nickname_of(event), auto=False
    )


async def auto_upgrade_credit(ctx, event):
    """连续升级信用等级直到余额不足。"""
    return await ctx.service.bank_upgrade(
        gid_of(event), uid_of(event), nickname_of(event), auto=True
    )


async def transfer(ctx, event):
    """转账给群友：转账 金额 @群友。"""
    ats = at_targets(event)
    if not ats:
        return notice("🚫", "请 @ 指定要转账的用户", [], tone="warn")
    m = re.search(rf"转账\s*(\d{{1,{MAX_ARG_LEN}}})", event.message_str or "")
    if not m:
        return notice("🚫", "请输入转账金额，如：转账 500 @群友", [], tone="warn")
    return await ctx.service.bank_transfer(
        gid_of(event), uid_of(event), nickname_of(event), ats[0], int(m.group(1))
    )


ROUTES = [
    Route(P + r"一键存款$", "cmd_deposit_all", "把所有现金一键存入银行", deposit_all),
    Route(P + r"存款([\s@\d]|$)", "cmd_deposit", "存款到银行", deposit),
    Route(P + r"取款([\s@\d]|$)", "cmd_withdraw", "从银行取款", withdraw),
    Route(P + r"银行信息$", "cmd_bank_info", "查看银行信息与可领利息", bank_info),
    Route(
        P + r"领取利息$", "cmd_collect_interest", "领取银行存款利息", collect_interest
    ),
    Route(
        P + r"一键升级信用$",
        "cmd_auto_upgrade_credit",
        "连续升级信用等级",
        auto_upgrade_credit,
    ),
    Route(P + r"升级信用$", "cmd_upgrade_credit", "升级信用等级", upgrade_credit),
    Route(P + r"转账([\s@\d]|$)", "cmd_transfer", "转账给群友", transfer),
]
