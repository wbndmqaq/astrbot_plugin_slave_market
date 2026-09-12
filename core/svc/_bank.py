"""银行与转账域（拆分自原 core/service.py）。"""

from __future__ import annotations

import math

from ..db import _NUM_CAP
from ..result import R, notice
from ._const import MAX_AUTO_UPGRADES, _fmt, _now


class _BankMixin:

    def _rate_cfg(self) -> tuple[float, int]:
        rate = self._num("bank", "interestRate")
        max_hours = self._int("bank", "maxInterestTime")
        return rate, max_hours

    def _pending_interest(self, data: dict, now: int) -> float:
        """当前可领利息（只读，不改存档）。"""
        bank = data["bank"]
        last = int(bank.get("lastInterestTime") or 0)
        if last <= 0 or last > now or bank["balance"] <= 0:
            return 0.0
        hours = (now - last) // 3600
        if hours < 1:
            return 0.0
        rate, max_hours = self._rate_cfg()
        return round(bank["balance"] * rate * min(hours, max_hours), 2)

    def _settle_interest(self, data: dict, now: int) -> float:
        """结算利息：发到 currency 并把计息起点推进到 now，返回实发利息。

        余额为 0 时也必须推进起点，否则"空账户静置 24 小时 → 一次性存满 → 立刻领息"
        就能拿到满额 24 小时利息，等于每天无风险 24% 且资金无需在行内停留。
        任何改动余额的操作（存/取）都要先调用它。
        """
        bank = data["bank"]
        last = int(bank.get("lastInterestTime") or 0)
        if last <= 0 or last > now:  # 首次使用银行 / 时钟回拨
            bank["lastInterestTime"] = now
            return 0.0
        if (now - last) // 3600 < 1:
            return 0.0
        interest = self._pending_interest(data, now)
        bank["lastInterestTime"] = now
        if interest > 0:
            data["currency"] = round(data["currency"] + interest, 2)
        return interest

    async def bank_deposit(
        self, group_id: str, user_id: str, nickname: str, amount: float | None
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._bank_deposit(tx, user_id, nickname, amount)
        )

    def _bank_deposit(
        self, tx, user_id: str, nickname: str, amount: float | None
    ) -> dict:
        # 参数校验先做：与 _bank_withdraw 一致，且无效指令不应推进计息起点
        all_in = amount is None
        if not all_in and (not isinstance(amount, (int, float)) or amount <= 0):
            return notice("🚫", "请输入正确的存款金额", [], tone="warn")
        data = tx.get(user_id, nickname)
        paid = self._settle_interest(data, _now())  # 校验通过后再结息
        if all_in:
            if data["currency"] <= 0:
                return notice("🚫", "你一分都没有，让我存寂寞", [], tone="warn")
            # 金币是浮点，用 round() 保留尾数，避免 int() 截断吞掉 0.7 之类的小数
            amount = round(data["currency"], 2)
        # round(99.999999, 2) = 100.00，但 data["currency"] = 99.999999
        # （浮点表示残留精度），导致 amount > currency 误判"余额不足"。
        # 钳到原值再比较
        if amount > data["currency"]:
            amount = math.floor(data["currency"] * 100) / 100
        if amount > data["currency"]:
            return notice("💸", "余额不足", [], tone="err")
        space = round(data["bank"]["limit"] - data["bank"]["balance"], 2)
        if amount > space:
            return notice(
                "🏦",
                "存款失败！超出存储上限",
                [
                    f"当前存储上限 {data['bank']['limit']} 金币，已存 {_fmt(data['bank']['balance'])}",
                    f"可存入 {_fmt(max(0.0, space))} 金币（可升级信用等级提升上限）",
                ],
                tone="warn",
            )
        data["currency"] = round(data["currency"] - amount, 2)
        data["bank"]["balance"] = round(data["bank"]["balance"] + amount, 2)
        lines = [
            f"存入 {_fmt(amount)} 金币",
            f"当前存款 {_fmt(data['bank']['balance'])}｜当前余额 {_fmt(data['currency'])}",
        ]
        if paid > 0:
            lines.append(f"（顺带结算了 {_fmt(paid)} 金币利息）")
        return notice("🏦", f"{'全部存入' if all_in else '存款'}成功！", lines)

    async def bank_withdraw(
        self, group_id: str, user_id: str, nickname: str, amount: int
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._bank_withdraw(tx, user_id, nickname, amount)
        )

    def _bank_withdraw(self, tx, user_id: str, nickname: str, amount: int) -> dict:
        if amount <= 0:  # 参数校验放在结息之前，避免无效指令也推进计息起点
            return notice("🚫", "请输入正确的取款金额", [], tone="warn")
        data = tx.get(user_id, nickname)
        paid = self._settle_interest(data, _now())
        if amount > data["bank"]["balance"]:
            return notice("💸", "存款余额不足", [], tone="err")
        data["currency"] = round(data["currency"] + amount, 2)
        data["bank"]["balance"] = round(data["bank"]["balance"] - amount, 2)
        lines = [
            f"当前存款 {_fmt(data['bank']['balance'])}",
            f"当前余额 {_fmt(data['currency'])}",
        ]
        if paid > 0:
            lines.append(f"（顺带结算了 {_fmt(paid)} 金币利息）")
        return notice("🏦", f"取款成功！取出 {_fmt(amount)} 金币", lines)

    async def bank_upgrade(
        self, group_id: str, user_id: str, nickname: str, auto: bool
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._bank_upgrade(tx, user_id, nickname, auto)
        )

    def _bank_upgrade(self, tx, user_id: str, nickname: str, auto: bool) -> dict:
        data = tx.get(user_id, nickname)
        self._settle_interest(data, _now())
        # 价格倍率必须 >1，否则升级费用不增长，一键升级会同步空转到破产
        price_multi = self._num("bank", "upgradePriceMulti")
        limit_multi = self._num("bank", "limitIncreaseMulti")

        upgrades, total_spent = 0, 0.0
        while upgrades < MAX_AUTO_UPGRADES:
            price = data["bank"]["upgradePrice"]
            if data["currency"] < price:
                break
            data["currency"] = round(data["currency"] - price, 2)
            total_spent += price
            upgrades += 1
            data["bank"]["level"] += 1
            # 钳到 SQLite INTEGER 64 位上限：玩家反复升级 + limit_multi>1 时
            # int(limit * 1.25) 在大数附近会触发 OverflowError 把整条指令搞崩
            data["bank"]["limit"] = min(
                _NUM_CAP, int(data["bank"]["limit"] * limit_multi)
            )
            data["bank"]["upgradePrice"] = max(
                price + 1, int(price * price_multi)
            )  # 保证严格递增
            if not auto:
                break

        if upgrades == 0:
            return notice(
                "💸",
                "升级失败！",
                [
                    f"当前升级需要 {data['bank']['upgradePrice']} 金币",
                    f"你的余额 {_fmt(data['currency'])} 金币",
                ],
                tone="err",
            )
        return notice(
            "📈",
            f"{'一键升级' if auto else '升级'}成功！共升级 {upgrades} 次，花费 {_fmt(total_spent)} 金币",
            [
                f"当前信用等级 Lv.{data['bank']['level']}｜存储上限 {data['bank']['limit']} 金币",
                f"下次升级费用 {data['bank']['upgradePrice']} 金币｜当前余额 {_fmt(data['currency'])}",
            ],
        )

    async def bank_info(self, group_id: str, user_id: str, nickname: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._bank_info(tx, user_id, nickname)
        )

    def _bank_info(self, tx, user_id: str, nickname: str) -> dict:
        data = tx.get(user_id, nickname)
        now = _now()
        bank = data["bank"]
        if int(bank.get("lastInterestTime") or 0) <= 0:
            bank["lastInterestTime"] = now
        rate, max_hours = self._rate_cfg()
        interest = self._pending_interest(data, now)
        text = (
            "===== 银行信息 =====\n"
            f"信用等级：Lv.{bank['level']}\n"
            f"当前存款：{_fmt(bank['balance'])} 金币\n"
            f"存储上限：{bank['limit']} 金币\n"
            f"升级费用：{bank['upgradePrice']} 金币\n"
            f"当前余额：{_fmt(data['currency'])} 金币\n"
            f"可领利息：{_fmt(interest)} 金币\n"
            f"利率说明：每小时 {rate * 100:.0f}%，最多计算 {max_hours} 小时"
        )
        return R(
            tmpl="bank",
            data={
                "level": bank["level"],
                "balance": _fmt(bank["balance"]),
                "limit": bank["limit"],
                "upgrade_price": bank["upgradePrice"],
                "currency": _fmt(data["currency"]),
                "interest": _fmt(interest),
                "rate": f"{rate * 100:.0f}%",
                "max_hours": max_hours,
            },
            text=text,
        )

    async def bank_interest(self, group_id: str, user_id: str, nickname: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._bank_interest(tx, user_id, nickname)
        )

    def _bank_interest(self, tx, user_id: str, nickname: str) -> dict:
        data = tx.get(user_id, nickname)
        interest = self._settle_interest(data, _now())
        if interest <= 0:
            return notice("⏳", "当前没有可领取的利息，每小时结算一次", [], tone="warn")
        return notice(
            "💰",
            f"成功领取利息 {_fmt(interest)} 金币",
            [
                f"当前存款 {_fmt(data['bank']['balance'])}",
                f"当前余额 {_fmt(data['currency'])}",
            ],
        )

    async def bank_transfer(
        self, group_id: str, user_id: str, nickname: str, target: str, amount: int
    ) -> dict:
        return await self.db.transact(
            group_id,
            lambda tx: self._bank_transfer(tx, user_id, nickname, target, amount),
        )

    def _bank_transfer(
        self, tx, user_id: str, nickname: str, target: str, amount: int
    ) -> dict:
        if str(target) == str(user_id):
            return notice("🚫", "不能给自己转账", [], tone="warn")
        data = tx.get(user_id, nickname)
        min_amount = self._int("transfer", "minAmount")
        fee_rate = self._num("transfer", "feeRate")
        if amount < min_amount:
            return notice("🚫", f"转账金额不能低于 {min_amount} 金币", [], tone="warn")
        if not tx.exists(target):
            return notice("🚫", "对方还没有参与游戏，无法转账", [], tone="warn")
        fee = math.ceil(amount * fee_rate)
        total = amount + fee
        if data["currency"] < total:
            return notice(
                "💸",
                "余额不足",
                [f"需要 {_fmt(total)} 金币（含手续费 {fee}）"],
                tone="err",
            )

        # 扣款与到账在同一事务里，中途异常整体回滚，钱不会凭空消失
        data["currency"] = round(data["currency"] - total, 2)
        recv = tx.get(target)
        recv["currency"] = round(recv["currency"] + amount, 2)
        return notice(
            "🤝",
            f"成功转账 {_fmt(amount)} 金币给 {self._name(recv, target)}",
            [f"手续费 {fee} 金币｜剩余余额 {_fmt(data['currency'])}"],
        )

    # ================= 查询 / 排行榜 =================
