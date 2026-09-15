"""抢劫域（拆分自原 core/service.py）。"""

from __future__ import annotations

import random

from ..result import notice
from ._const import _cd_text, _fmt, _now


class _RobMixin:

    async def rob(
        self, group_id: str, user_id: str, nickname: str, target: str | None
    ) -> dict:
        # 随机目标需要群成员名单：先在事务外取，事务内再校验目标是否真的有存档
        candidates: list[str] = []
        if not target:
            players = await self.db.list_players(group_id)
            me = await self.db.load(group_id, user_id)
            master = str(me.get("master") or "")
            candidates = [
                p for p in players if str(p) != str(user_id) and str(p) != master
            ]
            if not candidates:
                return notice(
                    "🕳️",
                    self.t(
                        "ui_rob_nobody",
                        "这个群里还没有其他人参与游戏，无处可抢",
                    ),
                    [],
                    tone="warn",
                )
        return await self.db.transact(
            group_id, lambda tx: self._rob(tx, user_id, nickname, target, candidates)
        )

    def _rob(
        self, tx, user_id: str, nickname: str, target: str | None, candidates: list[str]
    ) -> dict:
        data = tx.get(user_id, nickname)
        now = _now()
        cd = self._int("rob", "cooldown")
        left = self._cd_left(data, "lastRobTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳",
                self.t("ui_rob_cd", "抢劫冷却中"),
                [self.t("ui_cd_left", "剩余时间：{left}", left=_cd_text(left))],
                tone="warn",
            )

        if not target:
            target = random.choice(candidates)
        target = str(target)
        if target == str(user_id):
            return notice(
                "🚫", self.t("ui_rob_self", "你不能抢劫自己"), [], tone="warn"
            )
        if target == str(data["master"]):
            return notice(
                "🚫", self.t("ui_rob_master", "你不能抢劫你的主人"), [], tone="warn"
            )
        if not tx.exists(target):
            return notice(
                "🕳️",
                self.t("ui_rob_target_missing", "对方还没有参与游戏，无从下手"),
                [],
                tone="warn",
            )

        victim = tx.get(target)
        victim_name = self._name(victim, target)

        success_rate = self._num("rob", "successRate")
        penalty_rate = self._num("rob", "penalty")
        steal_rate = self._num("rob", "stealRate")
        max_steal = self._int("rob", "maxSteal")
        max_penalty = self._int("rob", "maxPenalty")
        if random.random() < success_rate:
            amount = round(min(victim["currency"] * steal_rate, max_steal), 2)
            data["currency"] = round(data["currency"] + amount, 2)
            victim["currency"] = round(max(0.0, victim["currency"] - amount), 2)
            result = notice(
                "🗡️",
                self.t("ui_rob_ok", "抢劫成功！"),
                [
                    self.t(
                        "ui_rob_gain",
                        "你从 {name} 那里抢到了 {amount} 金币",
                        name=victim_name,
                        amount=_fmt(amount),
                    )
                ],
            )
        else:
            amount = round(min(data["currency"] * penalty_rate, max_penalty), 2)
            data["currency"] = round(max(0.0, data["currency"] - amount), 2)
            result = notice(
                "🛡️",
                self.t("ui_rob_fail", "抢劫失败！"),
                [
                    self.t(
                        "ui_rob_fine", "你被罚了 {amount} 金币", amount=_fmt(amount)
                    )
                ],
                tone="err",
            )

        data["lastRobTime"] = now
        return result

    # ================= 训练 =================
