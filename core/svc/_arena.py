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
            return notice("🚫", "不能让同一个奴隶自己决斗", [], tone="warn")
        # 两个参战方都必须是自己的奴隶
        if not self._owns(data, sid1):
            return notice("🚫", "参战奴隶 1 不是你的奴隶", [], tone="warn")
        if not self._owns(data, sid2):
            return notice("🚫", "参战奴隶 2 不是你的奴隶", [], tone="warn")

        now = _now()
        cd = self._int("arena", "cooldown")
        left = self._cd_left(data, "lastBattleTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "决斗冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        fee = self._int("arena", "entryFee")
        if data["currency"] < fee:
            return notice(
                "💸",
                "余额不足",
                [f"参加决斗需要 {fee} 金币报名费，你只有 {_fmt(data['currency'])}"],
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
                "决斗动作文案未配置（arena_actions 为空），请在 WebUI 文案中补充",
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
        # 身价守恒：胜者涨幅取自败者跌幅与一个上限的较小值，
        # 否则 arena.cooldown=0 + 自己的两个奴隶互刷会让胜者身价单调上升无上限
        cap = int(self._num("arena", "maxWinBonus") * winner["value"])
        win_inc = min(win_inc, lose_dec, cap)
        winner["value"] = round(winner["value"] + win_inc, 2)
        loser["value"] = round(max(floor, loser["value"] - lose_dec), 2)
        data["currency"] = round(data["currency"] - fee + reward, 2)
        data["lastBattleTime"] = now
        if s1_wins:
            data["battleStats"]["wins"] += 1
        else:
            data["battleStats"]["losses"] += 1

        process = [_sample(actions) for _ in range(self._rand(2, 3))]
        text = (
            f"⚔️ 决斗开始：{n1} VS {n2}\n" + "\n".join(process) + "\n"
            f"决斗结束！{wn} 获胜！\n"
            f"{wn} 身价 +{_fmt(win_inc)} → {_fmt(winner['value'])}\n"
            f"{ln} 身价 -{_fmt(lose_dec)} → {_fmt(loser['value'])}\n"
            f"你支付报名费 {fee} 金币，获得奖励 {reward} 金币\n"
            f"战绩：{data['battleStats']['wins']} 胜 {data['battleStats']['losses']} 负"
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
                "lose_dec": _fmt(lose_dec),
                "loser_value": _fmt(loser["value"]),
                "fee": fee,
                "reward": reward,
                "wins": data["battleStats"]["wins"],
                "losses": data["battleStats"]["losses"],
            },
            text=text,
        )

    # ================= 排位赛 =================
