"""打工域（拆分自原 core/service.py）。"""

from __future__ import annotations

import random
import re

from ..result import R, notice
from ._const import _cd_text, _fmt, _now, _sample


class _WorkMixin:

    async def work(
        self, group_id: str, user_id: str, nickname: str, is_admin: bool
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._work(tx, user_id, nickname, is_admin)
        )

    def _work(self, tx, user_id: str, nickname: str, is_admin: bool) -> dict:
        data = tx.get(user_id, nickname)
        now = _now()
        # 拆成两个字面量分支：这样配置键与 schema 的 min/max 能被静态核对上
        cd = (
            self._int("work", "slaveownerCooldown")
            if is_admin
            else self._int("work", "cooldown")
        )
        left = self._cd_left(data, "lastWorkingTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "打工冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        value = data["value"]
        if not data["slave"]:
            if is_admin:
                lo = self._int("work", "slaveownerWageMin")
                hi = self._int("work", "slaveownerWageMax")
                wages = self._rand(lo, hi) + self._rand(int(value / 10), int(value / 5))
                text = _sample(self.copy["slaveowner"])
            else:
                lo = self._int("work", "wageMin")
                hi = self._int("work", "wageMax")
                wages = self._rand(lo, hi) + self._rand(
                    int(value / 20), int(value / 10)
                )
                text = _sample(self.copy["success"])
            data["currency"] = round(data["currency"] + wages, 2)
            data["lastWorkingTime"] = now
            # 提示语不要内联进 f-string 表达式：Python 3.12 之前
            # f-string 的表达式部分不允许出现反斜杠（\n），会是导入期 SyntaxError
            head = (
                "您是尊贵的奴隶主\n【您】"
                if is_admin
                else "你没有群友只能自己去打工\n【你】"
            )
            return R(
                tmpl="work",
                data={
                    "mode": "solo",
                    "is_admin": is_admin,
                    "story": text,
                    "wages": _fmt(wages),
                    "balance": _fmt(data["currency"]),
                },
                text=(f"{head}{text}{wages}金币\n当前共有{_fmt(data['currency'])}金币"),
            )

        # 有奴隶：让奴隶打工
        slave_lo = self._int("work", "slaveWageMin")
        slave_hi = self._int("work", "slaveWageMax")
        slack_rate = self._num("work", "slackRate")
        slack_loss = self._num("work", "slackValueLoss")
        lines, wages = [], 0
        for sid in [str(s) for s in data["slave"]]:
            slave = tx.get(sid)
            name = self._name(slave, sid)
            earn = self._rand(slave_lo, slave_hi) + self._rand(
                int(slave["value"] / 20), int(slave["value"] / 10)
            )
            if random.random() < slack_rate:  # 摸鱼
                old = slave["value"]
                slave["value"] = round(max(0.0, slave["value"] - slack_loss), 2)
                text = _sample(self.copy["failure"])
                text = (
                    text.replace("[A]", f"【{name}】")
                    .replace("[C]", _fmt(old))
                    .replace("[D]", _fmt(slave["value"]))
                )
                lines.append({"name": name, "story": text, "income": "0"})
            else:
                wages += earn
                lines.append(
                    {
                        "name": name,
                        "story": _sample(self.copy["success"]),
                        "income": str(earn),
                    }
                )

        data["currency"] = round(data["currency"] + wages, 2)
        data["lastWorkingTime"] = now
        expense = ""
        if random.random() < self._num("work", "expenseRate"):
            expense = _sample(self.copy["expenses"])
            m = re.search(r"\d+", expense)
            if m:
                cost = int(m.group())
                data["currency"] = round(max(0.0, data["currency"] - cost), 2)

        text = (
            f"💼 打工结果（总收入 {wages} 金币，当前共有 {_fmt(data['currency'])} 金币）\n"
            + "\n".join(
                f"【{it['name']}】{it['story']}{it['income']}金币" for it in lines
            )
        )
        if expense:
            text += f"\n💸 意外事件：{expense}"
        return R(
            tmpl="work",
            data={
                "mode": "team",
                "lines": lines,
                "wages": str(wages),
                "balance": _fmt(data["currency"]),
                "expense": expense,
            },
            text=text,
        )

    # ================= 购买 / 放生 / 赎身 =================
