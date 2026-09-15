"""购买 / 放生 / 赎身域（拆分自原 core/service.py）。"""

from __future__ import annotations

from ..result import notice
from ._const import _cd_text, _fmt, _iso_week, _now, _sample


class _PurchaseMixin:

    async def purchase(
        self, group_id: str, user_id: str, nickname: str, target: str
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._purchase(tx, user_id, nickname, target)
        )

    def _purchase(self, tx, user_id: str, nickname: str, target: str) -> dict:
        buyer = tx.get(user_id, nickname)
        if str(target) == str(user_id):
            return notice("🚫", self.t("ui_purchase_self", "不可以购买自己捏~"), [], tone="warn")
        # 必须已参与游戏：否则可以「购买」任意不存在的 ID，凭空建档并当作
        # 永久打工产线（对方不会赎身、不会被抢），还会污染市场与排行榜
        if not tx.exists(target):
            return notice(
                "🚫",
                self.t("ui_purchase_target_missing", "对方还没有参与游戏，无法购买"),
                [],
                tone="warn",
            )

        slave = tx.get(target)
        slave_name = self._name(slave, target)
        price = round(slave["value"], 2)
        former_owner_id = slave["master"]

        if former_owner_id == user_id:
            return notice(
                "🚫",
                self.t("ui_purchase_fail_title", "无法购买"),
                [
                    self.t(
                        "ui_purchase_already_master",
                        "你已经是 {name} 的主人了",
                        name=slave_name,
                    )
                ],
                tone="warn",
            )
        if str(target) == str(buyer["master"]):
            return notice("👑", _sample(self.copy.get("buyMaster") or []), [], tone="warn")
        if buyer["currency"] < price:
            return notice(
                "💸",
                self.t("ui_gold_short", "金币不足"),
                [
                    self.t(
                        "ui_purchase_short",
                        "购买 {name} 需要 {price} 金币，你只有 {have}",
                        name=slave_name,
                        price=_fmt(price),
                        have=_fmt(buyer["currency"]),
                    )
                ],
                tone="err",
            )

        now = _now()
        cd = self._int("purchase", "cooldown")
        left = self._cd_left(buyer, "lastPurchaseTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳",
                self.t("ui_purchase_cd", "购买冷却中"),
                [self.t("ui_cd_left", "剩余时间：{left}", left=_cd_text(left))],
                tone="warn",
            )

        # 扣款并登记奴隶
        buyer["currency"] = round(buyer["currency"] - price, 2)
        self._add_slave(buyer, target)
        buyer["lastPurchaseTime"] = now

        # 身价上涨、改换门庭
        gain = self._num("purchase", "valueGain")
        old_value = slave["value"]
        slave["value"] = round(slave["value"] + gain, 2)
        slave["master"] = str(user_id)

        # 一份钱只能进一个人的口袋：有原主人就付给原主人，无主则是"卖身钱"给本人。
        # 原主人已被删档（数据悬挂）时按无主处理——tx.get 对未知 uid 会返回
        # 新档模板，任何改动都会被 _flush 写成一条带赠送金币的幽灵行。
        lines = [
            self.t(
                "ui_purchase_paid",
                "花费 {cost} 金币，剩余 {left} 金币",
                cost=_fmt(price),
                left=_fmt(buyer["currency"]),
            )
        ]
        if (
            former_owner_id
            and former_owner_id != user_id
            and tx.exists(former_owner_id)
        ):
            former = tx.get(former_owner_id)
            former["currency"] = round(former["currency"] + price, 2)
            self._drop_slave(former, target)
            lines.append(
                self.t(
                    "ui_purchase_from_owner",
                    "已从 {name} 处购得，原主人收到了 {price} 金币",
                    name=self._name(former, former_owner_id),
                    price=_fmt(price),
                )
            )
        else:
            slave["currency"] = round(slave["currency"] + price, 2)
            lines.append(
                self.t(
                    "ui_purchase_sell_pay",
                    "{name} 拿到了 {price} 金币卖身钱",
                    name=slave_name,
                    price=_fmt(price),
                )
            )
        lines.append(
            self.t(
                "ui_purchase_value_up",
                "{name} 身价 {old} → {new}（+{gain}）",
                name=slave_name,
                old=_fmt(old_value),
                new=_fmt(slave["value"]),
                gain=_fmt(gain),
            )
        )
        return notice(
            "🛒",
            self.t("ui_purchase_ok", "成功购买了 {name}！", name=slave_name),
            lines,
        )

    async def release(self, group_id: str, user_id: str, target: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._release(tx, user_id, target)
        )

    def _release(self, tx, user_id: str, target: str) -> dict:
        data = tx.get(user_id)
        if not self._owns(data, target):
            return notice(
                "🚫", self.t("ui_not_owner", "你不是该奴隶的主人"), [], tone="warn"
            )
        self._drop_slave(data, target)
        slave = tx.get(target)
        # 只有确实归自己所有才解绑；否则仅清理自己列表里的悬挂 id
        if str(slave.get("master") or "") == str(user_id):
            slave["master"] = ""
        slave_name = self._name(slave, target)
        return notice(
            "🕊️",
            self.t("ui_release_ok", "成功放生了 {name}", name=slave_name),
            [
                self.t(
                    "ui_release_id", "用户 ID：{uid}，重获自由身", uid=str(target)
                )
            ],
        )

    async def buyback(self, group_id: str, user_id: str, nickname: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._buyback(tx, user_id, nickname)
        )

    def _buyback(self, tx, user_id: str, nickname: str) -> dict:
        data = tx.get(user_id, nickname)
        if not data["master"]:
            return notice(
            "🚫",
            self.t("ui_buyback_free", "你还没有主人，不需要赎身"),
            [],
            tone="warn",
        )

        now = _now()
        cd = self._int("buyBack", "cooldown")
        max_times = self._int("buyBack", "maxTimes")
        tax_rate = self._num("buyBack", "taxRate")
        price_multi = self._num("buyBack", "priceMulti")
        value_multi = self._num("buyBack", "valueIncreaseMulti")

        price = round(data["value"] * price_multi, 2)
        # 税按"赎身价"收，不是按剩余全部家当收
        tax = round(price * tax_rate, 2)
        total = round(price + tax, 2)
        if data["currency"] < total:
            return notice(
                "💸",
                self.t("ui_buyback_short_title", "买不起自己！"),
                [
                    self.t(
                        "ui_buyback_need",
                        "赎身需要 {price} 金币 + 税 {tax} = {total}",
                        price=_fmt(price),
                        tax=_fmt(tax),
                        total=_fmt(total),
                    ),
                    self.t(
                        "ui_you_have", "你只有 {have} 金币", have=_fmt(data["currency"])
                    ),
                ],
                tone="err",
            )

        # 先算冷却（内部会把"未来的时间戳"归零），再判周次：
        # 否则毫秒级/异常时间戳会让 _iso_week 抛异常且永远修不回来
        left = self._cd_left(data, "lastBuyBackTime", cd, user_id, now)
        # 跨周清零赎身次数：只比较 ISO(年,周) 是否变化
        if data["lastBuyBackTime"] and _iso_week(data["lastBuyBackTime"]) != _iso_week(
            now
        ):
            data["buyBackTimes"] = 0

        if data["buyBackTimes"] >= max_times:
            return notice(
            "🚫",
            self.t("ui_buyback_cap", "本周赎身次数已达上限，请下周再试"),
            [],
            tone="warn",
        )
        if left > 0:
            return notice(
                "⏳",
                self.t("ui_buyback_cd", "赎身冷却中"),
                [self.t("ui_cd_left", "剩余时间：{left}", left=_cd_text(left))],
                tone="warn",
            )

        master_id = data["master"]
        # 主人可能已被管理员删档（slave 列表里留下悬挂 id）：赎身照常办理，
        # 但绝不能给不存在的 uid 凭空建档送钱（tx.get 对未知 uid 返回新档
        # 模板，任何改动都会被 _flush 写成一条带赠送金币的幽灵行）。
        master_exists = tx.exists(master_id)

        data["currency"] = round(data["currency"] - total, 2)
        data["value"] = round(data["value"] * value_multi, 2)
        data["master"] = ""
        data["lastBuyBackTime"] = now
        data["buyBackTimes"] += 1

        lines = [
            self.t(
                "ui_buyback_tax",
                "缴纳税收 {tax} 金币，现余 {left} 金币",
                tax=_fmt(tax),
                left=_fmt(data["currency"]),
            ),
            self.t(
                "ui_buyback_value",
                "身价上涨至 {value} 金币",
                value=_fmt(data["value"]),
            ),
        ]
        if master_exists:
            master = tx.get(master_id)
            master["currency"] = round(master["currency"] + price, 2)
            self._drop_slave(master, user_id)
            lines.append(
                self.t(
                    "ui_buyback_paid",
                    "{name} 收到了 {price} 金币",
                    name=self._name(master, master_id),
                    price=_fmt(price),
                )
            )
        else:
            lines.append(
                self.t("ui_buyback_ghost", "原主人已不在游戏中，赎身金未支付")
            )

        return notice(
            "🔓",
            self.t(
                "ui_buyback_ok", "成功以 {price} 金币赎回了自己！", price=_fmt(price)
            ),
            lines,
        )

    # ================= 抢劫 =================
