"""决斗域（拆分自原 core/service.py）。"""

from __future__ import annotations

import random

from ..result import R, notice
from ._const import _cd_text, _fmt, _now, _sample


class _ArenaMixin:

    async def arena(
        self, group_id: str, user_id: str, nickname: str, sid1: str, sid2: str
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._arena(tx, user_id, nickname, sid1, sid2)
        )

    def _arena(self, tx, user_id: str, nickname: str, sid1: str, sid2: str) -> dict:
        data = tx.get(user_id, nickname)
        sid1, sid2 = str(sid1), str(sid2)
        if sid1 == sid2:
            return notice(
                "🚫",
                self.t("ui_arena_same", "不能让同一个奴隶自己决斗"),
                [],
                tone="warn",
            )
        # 两个参战方都必须是自己的奴隶
        if not self._owns(data, sid1):
            return notice(
                "🚫",
                self.t("ui_arena_not_own_1", "参战奴隶 1 不是你的奴隶"),
                [],
                tone="warn",
            )
        if not self._owns(data, sid2):
            return notice(
                "🚫",
                self.t("ui_arena_not_own_2", "参战奴隶 2 不是你的奴隶"),
                [],
                tone="warn",
            )

        now = _now()
        cd = self._int("arena", "cooldown")
        left = self._cd_left(data, "lastBattleTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳",
                self.t("ui_arena_cd", "决斗冷却中"),
                [self.t("ui_cd_left", "剩余时间：{left}", left=_cd_text(left))],
                tone="warn",
            )

        fee = self._int("arena", "entryFee")
        if data["currency"] < fee:
            return notice(
                "💸",
                self.t("ui_gold_short", "金币不足"),
                [
                    self.t(
                        "ui_arena_need",
                        "参加决斗需要 {fee} 金币报名费，你只有 {have}",
                        fee=fee,
                        have=_fmt(data["currency"]),
                    )
                ],
                tone="err",
            )

        s1 = tx.get(sid1)
        s2 = tx.get(sid2)
        n1 = self._name(s1, sid1)
        n2 = self._name(s2, sid2)

        # 动作文案缺失时决斗无法正常出结算，必须在扣费前拦截
        actions = [str(a) for a in (self.copy.get("arena_actions") or []) if str(a)]
        if not actions:
            return notice(
                "🚫",
                self.t(
                    "ui_arena_no_actions",
                    "决斗动作文案未配置（arena_actions 为空），请在 WebUI 文案中补充",
                ),
                [],
                tone="err",
            )

        # 身价差调整胜率（最高 ±30%）
        diff = s1["value"] - s2["value"]
        bonus = min(0.3, abs(diff) / max(s1["value"], s2["value"], 1) * 0.5)
        p1 = 0.5 + bonus if diff > 0 else 0.5 - bonus
        s1_wins = random.random() < p1

        winner, loser = (s1, s2) if s1_wins else (s2, s1)
        wn, ln = (n1, n2) if s1_wins else (n2, n1)

        # 奖励不能超过报名费，否则每打一次都净赚 = 无限刷币
        reward_rate = self._num("arena", "rewardRate")
        reward = int(fee * reward_rate)
        win_inc = int(winner["value"] * self._num("arena", "valueBonus"))
        lose_dec = int(loser["value"] * self._num("arena", "loseValueRate"))
        # 只保证"不因这次失败跌破下限"，不能无条件抬升：
        # 否则摸鱼掉到 60 的奴隶输一场反而涨回 100，可被用来洗身价
        floor = min(self._num("arena", "minValue"), loser["value"])
        # 败者实际跌幅先算：跌破下限时跌幅会被钳小，胜者涨幅必须按【实际
        # 跌幅】取，否则钳掉的差额凭空铸出（身价守恒被打破，每次触底决斗
        # 都是净通涨）。实际跌幅可能为 0（败者已在本档下限）→ 胜者不涨。
        actual_dec = loser["value"] - max(floor, loser["value"] - lose_dec)
        # 身价守恒：胜者涨幅取自败者实际跌幅与一个上限的较小值，
        # 否则 arena.cooldown=0 + 自己的两个奴隶互刷会让胜者身价单调上升无上限
        cap = int(self._num("arena", "maxWinBonus") * winner["value"])
        win_inc = min(win_inc, actual_dec, cap)
        winner["value"] = round(winner["value"] + win_inc, 2)
        loser["value"] = round(loser["value"] - actual_dec, 2)
        data["currency"] = round(data["currency"] - fee + reward, 2)
        data["lastBattleTime"] = now
        if s1_wins:
            data["battleStats"]["wins"] += 1
        else:
            data["battleStats"]["losses"] += 1

        process = [_sample(actions) for _ in range(self._rand(2, 3))]
        text = (
            self.t(
                "ui_arena_head",
                "⚔️ 决斗开始：{n1} VS {n2}",
                n1=n1,
                n2=n2,
            )
            + "\n"
            + "\n".join(process)
            + "\n"
            + self.t("ui_arena_win", "决斗结束！{name} 获胜！", name=wn)
            + "\n"
            + self.t(
                "ui_arena_winner_value",
                "{name} 身价 +{gain} → {value}",
                name=wn,
                gain=_fmt(win_inc),
                value=_fmt(winner["value"]),
            )
            + "\n"
            + self.t(
                "ui_arena_loser_value",
                "{name} 身价 -{loss} → {value}",
                name=ln,
                loss=_fmt(actual_dec),
                value=_fmt(loser["value"]),
            )
            + "\n"
            + self.t(
                "ui_arena_fee",
                "你支付报名费 {fee} 金币，获得奖励 {reward} 金币",
                fee=fee,
                reward=reward,
            )
            + "\n"
            + self.t(
                "ui_arena_record",
                "战绩：{wins} 胜 {losses} 负",
                wins=data["battleStats"]["wins"],
                losses=data["battleStats"]["losses"],
            )
        )
        return R(
            tmpl="arena",
            data={
                "n1": n1,
                "n2": n2,
                "v1": _fmt(s1["value"]),
                "v2": _fmt(s2["value"]),
                "process": process,
                "winner": wn,
                "loser": ln,
                "win_inc": _fmt(win_inc),
                "winner_value": _fmt(winner["value"]),
                "lose_dec": _fmt(actual_dec),
                "loser_value": _fmt(loser["value"]),
                "fee": fee,
                "reward": reward,
                "wins": data["battleStats"]["wins"],
                "losses": data["battleStats"]["losses"],
            },
            text=text,
        )

    # ================= 排位赛 =================
