"""训练域（拆分自原 core/service.py）。"""

from __future__ import annotations

import random

from ..result import R, notice
from ._const import _cd_text, _fmt, _now


class _TrainMixin:

    def _train_one(self, tx, user_id: str, data: dict, sid: str, now: int) -> dict:
        """训练单个奴隶（事务内）。会扣减 data["currency"]。"""
        cd = self._int("training", "cooldown")
        cost_rate = self._num("training", "costRate")
        inc_rate = self._num("training", "valueIncreaseRate")
        success_rate = self._num("training", "successRate")

        slave = tx.get(sid)
        name = self._name(slave, sid)
        left = self._cd_left(slave, "lastTrainedTime", cd, user_id, now)
        if left > 0:
            return {
                "name": name,
                "result": "休息中",
                "ok": None,
                "cost": 0,
                "detail": _cd_text(left),
            }
        cost = max(1, int(slave["value"] * cost_rate))  # 低身价奴隶也不能零成本刷
        if data["currency"] < cost:
            return {
                "name": name,
                "result": "金币不足",
                "ok": False,
                "cost": 0,
                "detail": f"需要 {_fmt(cost)} 金币",
            }

        data["currency"] = round(data["currency"] - cost, 2)
        slave["lastTrainedTime"] = now
        if random.random() < success_rate:
            inc = int(slave["value"] * inc_rate)
            slave["value"] = round(slave["value"] + inc, 2)
            return {
                "name": name,
                "result": "训练成功",
                "ok": True,
                "cost": cost,
                "detail": f"消耗 {_fmt(cost)}，身价 +{_fmt(inc)} → {_fmt(slave['value'])}",
            }
        return {
            "name": name,
            "result": "训练失败",
            "ok": False,
            "cost": cost,
            "detail": f"消耗 {_fmt(cost)}，身价未提升",
        }

    async def train(
        self, group_id: str, user_id: str, nickname: str, target: str
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._train(tx, user_id, nickname, target)
        )

    def _train(self, tx, user_id: str, nickname: str, target: str) -> dict:
        data = tx.get(user_id, nickname)
        if not self._owns(data, target):
            return notice("🚫", "你不是该奴隶的主人", [], tone="warn")
        r = self._train_one(tx, user_id, data, str(target), _now())
        return R(
            tmpl="train",
            data={"single": True, "results": [r], "balance": _fmt(data["currency"])},
            text=f"{r['name']}：{r['result']}（{r['detail']}）",
        )

    async def train_all(self, group_id: str, user_id: str, nickname: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._train_all(tx, user_id, nickname)
        )

    def _train_all(self, tx, user_id: str, nickname: str) -> dict:
        data = tx.get(user_id, nickname)
        if not data["slave"]:
            return notice("🚫", "你还没有奴隶可以训练", [], tone="warn")
        now = _now()
        results = [
            self._train_one(tx, user_id, data, str(sid), now)
            for sid in list(data["slave"])
        ]
        ok = sum(1 for r in results if r["result"] == "训练成功")
        spent = sum(r["cost"] for r in results)
        text = (
            f"🎯 一键训练完成（成功 {ok}/{len(results)}，总花费 {_fmt(spent)}，"
            f"当前余额 {_fmt(data['currency'])}）\n"
            + "\n".join(
                f"• {r['name']}：{r['result']}（{r['detail']}）" for r in results
            )
        )
        return R(
            tmpl="train",
            data={
                "single": False,
                "results": results,
                "ok": ok,
                "total": len(results),
                "spent": _fmt(spent),
                "balance": _fmt(data["currency"]),
            },
            text=text,
        )

    # ================= 决斗 =================
