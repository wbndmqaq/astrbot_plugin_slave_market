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
                "result": self.t("ui_train_rest", "休息中"),
                "ok": None,
                "cost": 0,
                "detail": _cd_text(left),
            }
        cost = max(1, int(slave["value"] * cost_rate))  # 低身价奴隶也不能零成本刷
        if data["currency"] < cost:
            return {
                "name": name,
                "result": self.t("ui_gold_short", "金币不足"),
                "ok": False,
                "cost": 0,
                "detail": self.t(
                    "ui_train_need", "需要 {cost} 金币", cost=_fmt(cost)
                ),
            }

        data["currency"] = round(data["currency"] - cost, 2)
        slave["lastTrainedTime"] = now
        if random.random() < success_rate:
            inc = int(slave["value"] * inc_rate)
            slave["value"] = round(slave["value"] + inc, 2)
            return {
                "name": name,
                "result": self.t("ui_train_ok", "训练成功"),
                "ok": True,
                "cost": cost,
                "detail": self.t(
                    "ui_train_gain",
                    "消耗 {cost}，身价 +{gain} → {value}",
                    cost=_fmt(cost),
                    gain=_fmt(inc),
                    value=_fmt(slave["value"]),
                ),
            }
        return {
            "name": name,
            "result": self.t("ui_train_fail", "训练失败"),
            "ok": False,
            "cost": cost,
            "detail": self.t(
                "ui_train_nogain", "消耗 {cost}，身价未提升", cost=_fmt(cost)
            ),
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
            return notice(
                "🚫", self.t("ui_not_owner", "你不是该奴隶的主人"), [], tone="warn"
            )
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
            return notice(
                "🚫",
                self.t("ui_train_noslave", "你还没有奴隶可以训练"),
                [],
                tone="warn",
            )
        now = _now()
        results = [
            self._train_one(tx, user_id, data, str(sid), now)
            for sid in list(data["slave"])
        ]
        ok = sum(1 for r in results if r["ok"] is True)
        spent = sum(r["cost"] for r in results)
        text = (
            self.t(
                "ui_train_all_head",
                "🎯 一键训练完成（成功 {ok}/{total}，总花费 {spent}，当前余额 {balance}）",
                ok=ok,
                total=len(results),
                spent=_fmt(spent),
                balance=_fmt(data["currency"]),
            )
            + "\n"
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
