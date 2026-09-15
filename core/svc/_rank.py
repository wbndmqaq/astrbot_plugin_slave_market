"""排位赛域（拆分自原 core/service.py）。

对手/事件/段位文案的唯一来源是 `copy`（resources/data/gameTexts.json，WebUI
可热更新）。返回内容可能为空，调用方必须先判空再使用，
绝不在代码里维护第二份副本。
"""

from __future__ import annotations

import math
import random

from ..result import R, notice
from ._const import _cd_text, _now, _sample


class _RankMixin:

    # 对手/事件/段位文案唯一来源是 copy（resources/data/gameTexts.json，WebUI 可热更新）。
    # 返回内容可能为空，调用方必须先判空再使用，绝不在代码里维护第二份副本。

    def _opponents(self) -> list[dict]:
        lst = self.copy.get("ranking_opponents") or []
        out = []
        for o in lst:
            if not isinstance(o, dict):
                continue
            try:
                out.append(
                    {
                        "name": str(o.get("name") or self.t("ui_rank_fallback_opponent", "对手")),
                        "score": int(o.get("score") or 0),
                        "specialEffect": str(o.get("specialEffect") or ""),
                    }
                )
            except (TypeError, ValueError):
                continue
        return out

    def _events(self) -> list[dict]:
        lst = self.copy.get("ranking_events") or []
        out = []
        for e in lst:
            if not isinstance(e, dict):
                continue
            try:
                out.append(
                    {
                        "name": str(e.get("name") or self.t("ui_rank_fallback_event", "事件")),
                        "effect": float(e.get("effect") or 1.0),
                        "desc": str(e.get("desc") or ""),
                    }
                )
            except (TypeError, ValueError):
                continue
        return out

    def _tiers(self) -> list[tuple[int, str]]:
        lst = self.copy.get("ranking_tiers") or []
        out = []
        for t in lst:
            try:
                out.append((int(t[0]), str(t[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def _top_tier(self) -> str:
        """顶级段位名（高于列表最高门槛）：唯一来源 gameTexts 的 ranking_top_tier。"""
        top = str(self.copy.get("ranking_top_tier") or "")
        return top or (self._tiers()[-1][1] if self._tiers() else "")

    def _tier(self, score: int) -> str:
        tiers = self._tiers()
        for threshold, name in tiers:
            if score < threshold:
                return name
        return self._top_tier()

    def _tier_desc(self) -> str:
        """段位说明文本：与 gameTexts 的 tiers / ranking_top_tier 保持一致（WebUI 改档位后自动同步）。"""
        tiers = self._tiers()
        if not tiers:
            return ""
        parts = [f"{name} <{threshold}" for threshold, name in tiers]
        parts.append(f"{self._top_tier()} ≥{tiers[-1][0]}")
        return "｜".join(parts)

    @staticmethod
    def _expected(score: int, opponent_score: int) -> float:
        """Elo 期望胜率。"""
        return 1 / (1 + 10 ** ((opponent_score - score) / 400))

    @staticmethod
    def _elo_diff(score: int, opponent_score: int, win: bool, k: int = 32) -> int:
        """Elo 分数变化：赢 +K(1-E)，输 -K·E（E 为本方期望胜率）。"""
        expected = _RankMixin._expected(score, opponent_score)
        diff = math.floor(k * (1 - expected)) if win else -math.floor(k * expected)
        if diff == 0:
            diff = 1 if win else -1
        return diff

    async def ranking_join(
        self, group_id: str, user_id: str, nickname: str, target: str
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._ranking_join(tx, user_id, nickname, target)
        )

    def _ranking_join(self, tx, user_id: str, nickname: str, target: str) -> dict:
        data = tx.get(user_id, nickname)
        if not self._owns(data, target):
            return notice(
                "🚫", self.t("ui_not_owner", "你不是该奴隶的主人"), [], tone="warn"
            )

        now = _now()
        cd = self._int("ranking", "cooldown")
        left = self._cd_left(data, "lastRankingTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳",
                self.t("ui_rank_cd", "排位赛冷却中"),
                [self.t("ui_cd_left", "剩余时间：{left}", left=_cd_text(left))],
                tone="warn",
            )

        slave = tx.get(target)
        slave_name = self._name(slave, target)
        score = slave["ranking"]["score"]

        # 文案唯一来源是 gameTexts.json；缺失时静默降级为提示，不写任何数据
        events = self._events()
        opponents = self._opponents()
        tiers = self._tiers()
        if not events or not opponents or not tiers:
            return notice(
                "🚫",
                self.t(
                    "ui_rank_no_copy",
                    "排位赛文案未配置（gameTexts 的 events / opponents / tiers 为空），"
                    "请在 WebUI 文案中补充",
                ),
                [],
                tone="err",
            )

        event = _sample(events)
        valid = [o for o in opponents if abs(o["score"] - score) <= 300] or opponents
        opponent = _sample(valid)

        # 胜率以 Elo 期望胜率为基准再乘事件系数
        win_rate = min(
            0.95, max(0.05, self._expected(score, opponent["score"]) * event["effect"])
        )
        win = random.random() < win_rate
        diff = self._elo_diff(score, opponent["score"], win)

        slave["ranking"]["score"] = max(0, score + diff)
        slave["ranking"]["matches"] += 1
        slave["ranking"]["tier"] = self._tier(slave["ranking"]["score"])
        # 只有赢了才有奖励，输了不发钱
        reward_rate = self._num("ranking", "rewardRate")
        reward = int(abs(diff) * reward_rate) if win else 0
        data["currency"] = round(data["currency"] + reward, 2)
        data["lastRankingTime"] = now

        sign = "+" if diff > 0 else ""
        text = (
            self.t(
                "ui_rank_event",
                "🏆 排位赛：当前事件「{event}」（{desc}）",
                event=event["name"],
                desc=event["desc"],
            )
            + "\n"
            + self.t(
                "ui_rank_vs",
                "{name} VS {opponent}（对手特性：{effect}）",
                name=slave_name,
                opponent=opponent["name"],
                effect=opponent["specialEffect"],
            )
            + "\n"
            + (self.t("ui_rank_win", "胜利！") if win else self.t("ui_rank_lose", "失败！"))
            + "\n"
            + self.t(
                "ui_rank_diff",
                "分数变化：{sign}{diff}，当前 {score} 分",
                sign=sign,
                diff=diff,
                score=slave["ranking"]["score"],
            )
            + "\n"
            + self.t(
                "ui_rank_tier", "当前段位：{tier}", tier=slave["ranking"]["tier"]
            )
            + "\n"
            + self.t("ui_rank_reward", "获得奖励：{reward} 金币", reward=reward)
        )
        return R(
            tmpl="rank_match",
            data={
                "name": slave_name,
                "event": event,
                "opponent": opponent,
                "win": win,
                "diff": diff,
                "score": slave["ranking"]["score"],
                "tier": slave["ranking"]["tier"],
                "tier_desc": self._tier_desc(),
                "matches": slave["ranking"]["matches"],
                "reward": reward,
            },
            text=text,
        )

    async def ranking_show(self, group_id: str, user_id: str, nickname: str) -> dict:
        data = await self.get_player(group_id, user_id, nickname)
        if not data["slave"]:
            return notice(
                "🚫",
                self.t("ui_rank_noslave", "你还没有奴隶，无法查看排位赛信息"),
                [],
                tone="warn",
            )
        # 一次性把全部奴隶的排行信息查回来：避免 N 次 self.db.load 的
        # SQLite 开/闭开销；用 list 转 dict 也减少下游查找的 O(n^2)。
        slave_ids = [str(s) for s in data["slave"]]
        rows = await self.db.query_players_by_uids(group_id, slave_ids)
        slaves_by_id = dict(rows)
        rows = []
        for sid in slave_ids:
            slave = slaves_by_id.get(sid)
            if slave is None:
                # 期间被删档：跳过
                continue
            r = slave["ranking"]
            rows.append(
                {
                    "name": self._name(slave, sid),
                    "tier": r["tier"],
                    "score": r["score"],
                    "matches": r["matches"],
                }
            )
        text = self.t("ui_rank_info_head", "【奴隶排位赛信息】") + "\n" + "\n".join(
            self.t(
                "ui_rank_info_row",
                "{name}：段位 {tier}｜分数 {score}｜场次 {matches}",
                name=r["name"],
                tier=r["tier"],
                score=r["score"],
                matches=r["matches"],
            )
            for r in rows
        )
        td = self._tier_desc()
        if td:
            text += "\n" + self.t("ui_rank_tier_desc_head", "【段位说明】") + td
        return R(
            tmpl="rank_match",
            data={"info_mode": True, "rows": rows, "tier_desc": td},
            text=text,
        )

    # ================= 银行 =================
