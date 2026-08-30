"""奴隶市场游戏逻辑层。

所有函数均为纯异步；返回统一为 result.R 结构：
- err：用户可见错误（优先输出）
- tmpl+data：HTML 模板渲染（Playwright）
- text：纯文本回退（渲染失败/关闭图片时使用）

事务约定（重要）：
    任何会改动金币/身价/主奴关系的操作都必须写成 `def _xxx(self, tx, ...)` 同步函数，
    再由 `db.transact()` 在单线程、单锁、单事务里执行。tx.get(uid) 取存档、原地改，
    事务结束时只写回真正变化的行。**tx 内禁止 await**（会与自身持有的锁互等）。
    这样才能杜绝"load 与 save 之间被另一条指令插入"导致的整行覆盖与丢失更新。

内部异常向上抛出，由 handlers/main 统一捕获。
"""

from __future__ import annotations

import math
import random
import re
from datetime import datetime
from typing import ClassVar


try:  # 兼容包加载与 sys.path 加载两种方式
    from .db import PlayerDB, _NUM_CAP
    from .result import R, notice
except ImportError:  # pragma: no cover
    from db import PlayerDB, _NUM_CAP  # type: ignore
    from result import R, notice  # type: ignore

# 一键升级信用的单次上限：防止配置成 upgradePriceMulti<=1 时同步空转卡死事件循环
MAX_AUTO_UPGRADES = 100
# 列表/排行榜单次返回上限
MARKET_LIMIT = 100
BOARD_LIMIT = 15
# 全量扫描型榜单（slave/bank）单次最多读多少行：超出只能近似截断，
# 防止异常膨胀的库把整表读进内存
FULL_SCAN_CAP = 10000


def _fmt(x: float) -> str:
    return f"{float(x):.2f}"


def _cd_text(seconds: int) -> str:
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _sample(lst: list) -> str:
    return random.choice(lst)


def _now() -> int:
    return int(datetime.now().timestamp())


def _iso_week(ts: int) -> tuple[int, int]:
    """时间戳 -> (ISO 年, ISO 周)。跨年时第 52/53 周与第 1 周也能正确区分。

    坏时间戳（毫秒级、超范围）不能让指令直接报错，回退成 (0, 0)。
    """
    try:
        c = datetime.fromtimestamp(int(ts)).isocalendar()
    except (ValueError, OSError, OverflowError, TypeError):
        return (0, 0)
    return (c[0], c[1])


class GameService:
    def __init__(self, db: PlayerDB, config: dict, copywriting: dict):
        self.db = db
        self.config = config
        self.copy = copywriting

    # ================= 基础工具 =================

    def _cfg(self, *path, default=None):
        node = self.config
        for p in path:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        return node if node != {} else default

    def _num(self, *path, default: float, lo: float, hi: float) -> float:
        """读浮点配置并夹到 [lo, hi]：配置写错（如概率填 5）不该变成必中/无限刷。"""
        try:
            v = float(self._cfg(*path, default=default))
        except (TypeError, ValueError):
            v = float(default)
        if not math.isfinite(v):
            v = float(default)
        return min(hi, max(lo, v))

    def _int(self, *path, default: int, lo: int, hi: int) -> int:
        return int(self._num(*path, default=default, lo=lo, hi=hi))

    @staticmethod
    def _rand(lo: int, hi: int) -> int:
        """闭区间随机整数。配置把上下限填反时自动交换，不让 randint 抛异常。"""
        return random.randint(min(lo, hi), max(lo, hi))

    def _no_cd(self, user_id: str) -> bool:
        return str(user_id) in {str(u) for u in self.config.get("ignoreCDUsers", []) or []}

    def _cd_left(self, data: dict, key: str, cd: int, user_id: str, now: int) -> int:
        """统一冷却计算。返回剩余秒数（0 表示可以执行）。

        系统时钟回拨（容器时间同步、跨机迁移）会让存档里的时间戳大于当前时间，
        旧写法 `cd - (now - last)` 会算出一个巨大的正数把玩家锁死，这里把
        "未来的时间戳"直接当作 0 处理。
        """
        if self._no_cd(user_id):
            return 0
        last = int(data.get(key) or 0)
        if last > now:  # 时钟回拨：重置而不是锁死
            data[key] = 0
            return 0
        left = cd - (now - last)
        return left if left > 0 else 0

    async def get_player(self, group_id: str, user_id: str, nickname: str = "") -> dict:
        """只读取存档。昵称更新走 set_card（只 UPDATE nickname 列）。

        早期版本这里是 load + save 整行回写，两个 await 之间别人对这一行的
        改动（被抢劫、收到转账、被购买）会被旧快照整行覆盖，等于凭空造币或丢钱。
        """
        data = await self.db.load(group_id, user_id)
        if nickname and nickname != data.get("nickname", ""):
            await self.db.set_card(group_id, user_id, nickname)
            data["nickname"] = nickname
        return data

    async def name_of(self, group_id: str, user_id: str) -> str:
        data = await self.db.load(group_id, user_id)
        return data.get("nickname") or f"用户{user_id}"

    @staticmethod
    def _name(data: dict, uid: str) -> str:
        return data.get("nickname") or f"用户{uid}"

    @staticmethod
    def _owns(data: dict, uid: str) -> bool:
        """uid 是否在 data 的奴隶列表里（列表统一存字符串）。"""
        return str(uid) in {str(s) for s in data.get("slave") or []}

    @staticmethod
    def _drop_slave(data: dict, uid: str) -> None:
        data["slave"] = [s for s in data["slave"] if str(s) != str(uid)]

    @staticmethod
    def _add_slave(data: dict, uid: str) -> None:
        data["slave"] = sorted({*(str(s) for s in data["slave"]), str(uid)})

    # ================= 打工 =================

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
            self._int("work", "slaveownerCooldown", default=60, lo=0, hi=604800)
            if is_admin
            else self._int("work", "cooldown", default=3600, lo=0, hi=604800)
        )
        left = self._cd_left(data, "lastWorkingTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "打工冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        value = data["value"]
        if not data["slave"]:
            if is_admin:
                lo = self._int("work", "slaveownerWageMin", default=100, lo=0, hi=10**9)
                hi = self._int("work", "slaveownerWageMax", default=2000, lo=0, hi=10**9)
                wages = self._rand(lo, hi) + self._rand(
                    int(value / 10), int(value / 5)
                )
                text = _sample(self.copy["slaveowner"])
            else:
                lo = self._int("work", "wageMin", default=10, lo=0, hi=10**9)
                hi = self._int("work", "wageMax", default=100, lo=0, hi=10**9)
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
                text=(
                    f"{head}{text}{wages}金币\n"
                    f"当前共有{_fmt(data['currency'])}金币"
                ),
            )

        # 有奴隶：让奴隶打工
        slave_lo = self._int("work", "slaveWageMin", default=5, lo=0, hi=10**9)
        slave_hi = self._int("work", "slaveWageMax", default=20, lo=0, hi=10**9)
        slack_rate = self._num("work", "slackRate", default=0.1, lo=0.0, hi=1.0)
        slack_loss = self._num("work", "slackValueLoss", default=20.0, lo=0.0, hi=10**6)
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
        if random.random() < self._num("work", "expenseRate", default=0.2, lo=0.0, hi=1.0):
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

    async def purchase(
        self, group_id: str, user_id: str, nickname: str, target: str
    ) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._purchase(tx, user_id, nickname, target)
        )

    def _purchase(self, tx, user_id: str, nickname: str, target: str) -> dict:
        buyer = tx.get(user_id, nickname)
        if str(target) == str(user_id):
            return notice("🚫", "不可以购买自己捏~", [], tone="warn")
        # 必须已参与游戏：否则可以「购买」任意不存在的 ID，凭空建档并当作
        # 永久打工产线（对方不会赎身、不会被抢），还会污染市场与排行榜
        if not tx.exists(target):
            return notice("🚫", "对方还没有参与游戏，无法购买", [], tone="warn")

        slave = tx.get(target)
        slave_name = self._name(slave, target)
        price = round(slave["value"], 2)
        former_owner_id = slave["master"]

        if former_owner_id == user_id:
            return notice(
                "🚫", "无法购买", [f"你已经是 {slave_name} 的主人了"], tone="warn"
            )
        if str(target) == str(buyer["master"]):
            return notice("👑", _sample(self.copy["buyMaster"]), [], tone="warn")
        if buyer["currency"] < price:
            return notice(
                "💸",
                "金币不足",
                [
                    f"购买 {slave_name} 需要 {_fmt(price)} 金币，你只有 {_fmt(buyer['currency'])}"
                ],
                tone="err",
            )

        now = _now()
        cd = self._int("purchase", "cooldown", default=3600, lo=0, hi=604800)
        left = self._cd_left(buyer, "lastPurchaseTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "购买冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        # 扣款并登记奴隶
        buyer["currency"] = round(buyer["currency"] - price, 2)
        self._add_slave(buyer, target)
        buyer["lastPurchaseTime"] = now

        # 身价上涨、改换门庭
        gain = self._num("purchase", "valueGain", default=20.0, lo=0.0, hi=10**6)
        old_value = slave["value"]
        slave["value"] = round(slave["value"] + gain, 2)
        slave["master"] = str(user_id)

        # 一份钱只能进一个人的口袋：有原主人就付给原主人，无主则是"卖身钱"给本人。
        # 旧版同时给奴隶和原主人各付一份 price，等于每笔交易凭空增发一份货币。
        lines = [f"花费 {_fmt(price)} 金币，剩余 {_fmt(buyer['currency'])} 金币"]
        if former_owner_id and former_owner_id != user_id:
            former = tx.get(former_owner_id)
            former["currency"] = round(former["currency"] + price, 2)
            self._drop_slave(former, target)
            lines.append(
                f"已从 {self._name(former, former_owner_id)} 处购得，"
                f"原主人收到了 {_fmt(price)} 金币"
            )
        else:
            slave["currency"] = round(slave["currency"] + price, 2)
            lines.append(f"{slave_name} 拿到了 {_fmt(price)} 金币卖身钱")
        lines.append(
            f"{slave_name} 身价 {_fmt(old_value)} → {_fmt(slave['value'])}"
            f"（+{_fmt(gain)}）"
        )
        return notice("🛒", f"成功购买了 {slave_name}！", lines)

    async def release(self, group_id: str, user_id: str, target: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._release(tx, user_id, target)
        )

    def _release(self, tx, user_id: str, target: str) -> dict:
        data = tx.get(user_id)
        if not self._owns(data, target):
            return notice("🚫", "你不是该奴隶的主人", [], tone="warn")
        self._drop_slave(data, target)
        slave = tx.get(target)
        # 只有确实归自己所有才解绑；否则仅清理自己列表里的悬挂 id
        if str(slave.get("master") or "") == str(user_id):
            slave["master"] = ""
        slave_name = self._name(slave, target)
        return notice(
            "🕊️", f"成功放生了 {slave_name}", [f"用户 ID：{target}，重获自由身"]
        )

    async def buyback(self, group_id: str, user_id: str, nickname: str) -> dict:
        return await self.db.transact(
            group_id, lambda tx: self._buyback(tx, user_id, nickname)
        )

    def _buyback(self, tx, user_id: str, nickname: str) -> dict:
        data = tx.get(user_id, nickname)
        if not data["master"]:
            return notice("🚫", "你还没有主人，不需要赎身", [], tone="warn")

        now = _now()
        cd = self._int("buyBack", "cooldown", default=86400, lo=0, hi=2592000)
        max_times = self._int("buyBack", "maxTimes", default=3, lo=1, hi=1000)
        tax_rate = self._num("buyBack", "taxRate", default=0.05, lo=0.0, hi=1.0)
        price_multi = self._num("buyBack", "priceMulti", default=2.0, lo=0.1, hi=100.0)
        value_multi = self._num(
            "buyBack", "valueIncreaseMulti", default=1.2, lo=1.0, hi=10.0
        )

        price = round(data["value"] * price_multi, 2)
        # 税按"赎身价"收，不是按剩余全部家当收（旧写法让富人多缴几万）
        tax = round(price * tax_rate, 2)
        total = round(price + tax, 2)
        if data["currency"] < total:
            return notice(
                "💸",
                "买不起自己！",
                [
                    f"赎身需要 {_fmt(price)} 金币 + 税 {_fmt(tax)} = {_fmt(total)}",
                    f"你只有 {_fmt(data['currency'])} 金币",
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
            return notice("🚫", "本周赎身次数已达上限，请下周再试", [], tone="warn")
        if left > 0:
            return notice(
                "⏳", "赎身冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        master_id = data["master"]
        master = tx.get(master_id)

        data["currency"] = round(data["currency"] - total, 2)
        data["value"] = round(data["value"] * value_multi, 2)
        data["master"] = ""
        data["lastBuyBackTime"] = now
        data["buyBackTimes"] += 1

        master["currency"] = round(master["currency"] + price, 2)
        self._drop_slave(master, user_id)

        return notice(
            "🔓",
            f"成功以 {_fmt(price)} 金币赎回了自己！",
            [
                f"缴纳税收 {_fmt(tax)} 金币，现余 {_fmt(data['currency'])} 金币",
                f"身价上涨至 {_fmt(data['value'])} 金币",
                f"{self._name(master, master_id)} 收到了 {_fmt(price)} 金币",
            ],
        )

    # ================= 抢劫 =================

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
                    "🕳️", "这个群里还没有其他人参与游戏，无处可抢", [], tone="warn"
                )
        return await self.db.transact(
            group_id, lambda tx: self._rob(tx, user_id, nickname, target, candidates)
        )

    def _rob(
        self, tx, user_id: str, nickname: str, target: str | None, candidates: list[str]
    ) -> dict:
        data = tx.get(user_id, nickname)
        now = _now()
        cd = self._int("rob", "cooldown", default=600, lo=0, hi=604800)
        left = self._cd_left(data, "lastRobTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "抢劫冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        if not target:
            target = random.choice(candidates)
        target = str(target)
        if target == str(user_id):
            return notice("🚫", "你不能抢劫自己", [], tone="warn")
        if target == str(data["master"]):
            return notice("🚫", "你不能抢劫你的主人", [], tone="warn")
        if not tx.exists(target):
            return notice("🕳️", "对方还没有参与游戏，无从下手", [], tone="warn")

        victim = tx.get(target)
        victim_name = self._name(victim, target)

        success_rate = self._num("rob", "successRate", default=0.3, lo=0.0, hi=1.0)
        penalty_rate = self._num("rob", "penalty", default=0.1, lo=0.0, hi=1.0)
        steal_rate = self._num("rob", "stealRate", default=0.2, lo=0.0, hi=1.0)
        max_steal = self._int("rob", "maxSteal", default=100, lo=0, hi=10**9)
        max_penalty = self._int("rob", "maxPenalty", default=50, lo=0, hi=10**9)
        if random.random() < success_rate:
            amount = round(min(victim["currency"] * steal_rate, max_steal), 2)
            data["currency"] = round(data["currency"] + amount, 2)
            victim["currency"] = round(max(0.0, victim["currency"] - amount), 2)
            result = notice(
                "🗡️",
                "抢劫成功！",
                [f"你从 {victim_name} 那里抢到了 {_fmt(amount)} 金币"],
            )
        else:
            amount = round(min(data["currency"] * penalty_rate, max_penalty), 2)
            data["currency"] = round(max(0.0, data["currency"] - amount), 2)
            result = notice(
                "🛡️", "抢劫失败！", [f"你被罚了 {_fmt(amount)} 金币"], tone="err"
            )

        data["lastRobTime"] = now
        return result

    # ================= 训练 =================

    def _train_one(self, tx, user_id: str, data: dict, sid: str, now: int) -> dict:
        """训练单个奴隶（事务内）。会扣减 data["currency"]。"""
        cd = self._int("training", "cooldown", default=7200, lo=0, hi=604800)
        cost_rate = self._num("training", "costRate", default=0.1, lo=0.01, hi=2.0)
        inc_rate = self._num("training", "valueIncreaseRate", default=0.2, lo=0.0, hi=1.0)
        success_rate = self._num("training", "successRate", default=0.7, lo=0.0, hi=1.0)

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
        # 两个参战方都必须是自己的奴隶：旧版不校验 sid2，可以拉任意群友当陪练并扣他身价
        if not self._owns(data, sid1):
            return notice("🚫", "参战奴隶 1 不是你的奴隶", [], tone="warn")
        if not self._owns(data, sid2):
            return notice("🚫", "参战奴隶 2 不是你的奴隶", [], tone="warn")

        now = _now()
        cd = self._int("arena", "cooldown", default=7200, lo=0, hi=604800)
        left = self._cd_left(data, "lastBattleTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "决斗冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        fee = self._int("arena", "entryFee", default=50, lo=0, hi=10**9)
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

        # 身价差调整胜率（最高 ±30%）
        diff = s1["value"] - s2["value"]
        bonus = min(0.3, abs(diff) / max(s1["value"], s2["value"], 1) * 0.5)
        p1 = 0.5 + bonus if diff > 0 else 0.5 - bonus
        s1_wins = random.random() < p1

        winner, loser = (s1, s2) if s1_wins else (s2, s1)
        wn, ln = (n1, n2) if s1_wins else (n2, n1)

        # 奖励不能超过报名费，否则每打一次都净赚 = 无限刷币
        reward_rate = self._num("arena", "rewardRate", default=0.2, lo=0.0, hi=1.0)
        reward = int(fee * reward_rate)
        win_inc = int(
            winner["value"] * self._num("arena", "valueBonus", default=0.1, lo=0.0, hi=1.0)
        )
        lose_dec = int(
            loser["value"] * self._num("arena", "loseValueRate", default=0.05, lo=0.0, hi=1.0)
        )
        # 只保证"不因这次失败跌破下限"，不能无条件抬升：
        # 否则摸鱼掉到 60 的奴隶输一场反而涨回 100，可被用来洗身价
        floor = min(
            self._num("arena", "minValue", default=100.0, lo=0.0, hi=10**6),
            loser["value"],
        )
        # 身价守恒：胜者涨幅取自败者跌幅与一个上限的较小值，
        # 否则 arena.cooldown=0 + 自己的两个奴隶互刷会让胜者身价单调上升无上限
        cap = int(
            self._num("arena", "maxWinBonus", default=0.2, lo=0.0, hi=1.0)
            * winner["value"]
        )
        win_inc = min(win_inc, lose_dec, cap)
        winner["value"] = round(winner["value"] + win_inc, 2)
        loser["value"] = round(max(floor, loser["value"] - lose_dec), 2)
        data["currency"] = round(data["currency"] - fee + reward, 2)
        data["lastBattleTime"] = now
        if s1_wins:
            data["battleStats"]["wins"] += 1
        else:
            data["battleStats"]["losses"] += 1

        actions = [
            f"{n1}使出浑身解数",
            f"{n2}奋力反击",
            f"{n1}展开猛攻",
            f"{n2}寻找破绽",
            f"{n1}气势如虹",
            f"{n2}毫不示弱",
        ]
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

    _OPPONENTS: ClassVar[list[dict]] = [
        {"name": "流浪剑客", "score": 800, "specialEffect": "剑术精湛，容易造成暴击"},
        {"name": "江湖大侠", "score": 1200, "specialEffect": "内力深厚，防御力强"},
        {"name": "武林高手", "score": 1600, "specialEffect": "轻功绝顶，闪避率高"},
        {"name": "绝世高手", "score": 2000, "specialEffect": "武学通神，全面强化"},
        {"name": "隐世门派弟子", "score": 1400, "specialEffect": "招式诡异，难以预测"},
        {"name": "江湖杀手", "score": 1100, "specialEffect": "出手狠辣，伤害提升"},
        {"name": "武馆教习", "score": 900, "specialEffect": "经验丰富，稳扎稳打"},
        {"name": "散打高手", "score": 1300, "specialEffect": "近身搏斗见长"},
    ]
    _EVENTS: ClassVar[list[dict]] = [
        {"name": "天气晴朗", "effect": 1.1, "desc": "状态绝佳"},
        {"name": "狂风暴雨", "effect": 0.9, "desc": "行动受限"},
        {"name": "月黑风高", "effect": 1.2, "desc": "战力提升"},
        {"name": "人来人往", "effect": 0.95, "desc": "注意力分散"},
        {"name": "良辰吉日", "effect": 1.15, "desc": "运势加成"},
    ]
    _TIERS: ClassVar[list[tuple[int, str]]] = [
        (1000, "青铜"),
        (1400, "白银"),
        (1800, "黄金"),
        (2200, "铂金"),
    ]

    @classmethod
    def _tier(cls, score: int) -> str:
        for threshold, name in cls._TIERS:
            if score < threshold:
                return name
        return "钻石"

    @staticmethod
    def _expected(score: int, opponent_score: int) -> float:
        """Elo 期望胜率。"""
        return 1 / (1 + 10 ** ((opponent_score - score) / 400))

    @classmethod
    def _elo_diff(cls, score: int, opponent_score: int, win: bool, k: int = 32) -> int:
        """Elo 分数变化：赢 +K(1-E)，输 -K·E（E 为本方期望胜率）。"""
        expected = cls._expected(score, opponent_score)
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
            return notice("🚫", "你不是该奴隶的主人", [], tone="warn")

        now = _now()
        cd = self._int("ranking", "cooldown", default=3600, lo=0, hi=604800)
        left = self._cd_left(data, "lastRankingTime", cd, user_id, now)
        if left > 0:
            return notice(
                "⏳", "排位赛冷却中", [f"剩余时间：{_cd_text(left)}"], tone="warn"
            )

        slave = tx.get(target)
        slave_name = self._name(slave, target)
        score = slave["ranking"]["score"]

        event = _sample(self._EVENTS)
        valid = [
            o for o in self._OPPONENTS if abs(o["score"] - score) <= 300
        ] or self._OPPONENTS
        opponent = _sample(valid)

        # 胜率以 Elo 期望胜率为基准再乘事件系数：旧写法固定 0.5×effect，
        # 与对手强弱完全无关，导致分数无上界单调漂移、段位失去意义
        win_rate = min(0.95, max(0.05, self._expected(score, opponent["score"]) * event["effect"]))
        win = random.random() < win_rate
        diff = self._elo_diff(score, opponent["score"], win)

        slave["ranking"]["score"] = max(0, score + diff)
        slave["ranking"]["matches"] += 1
        slave["ranking"]["tier"] = self._tier(slave["ranking"]["score"])
        # 只有赢了才有奖励，输了不发钱
        reward_rate = self._num("ranking", "rewardRate", default=0.1, lo=0.0, hi=5.0)
        reward = int(abs(diff) * reward_rate) if win else 0
        data["currency"] = round(data["currency"] + reward, 2)
        data["lastRankingTime"] = now

        text = (
            f"🏆 排位赛：当前事件「{event['name']}」（{event['desc']}）\n"
            f"{slave_name} VS {opponent['name']}（对手特性：{opponent['specialEffect']}）\n"
            f"{'胜利！' if win else '失败！'}\n"
            f"分数变化：{'+' if diff > 0 else ''}{diff}，当前 {slave['ranking']['score']} 分\n"
            f"当前段位：{slave['ranking']['tier']}\n"
            f"获得奖励：{reward} 金币"
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
                "matches": slave["ranking"]["matches"],
                "reward": reward,
            },
            text=text,
        )

    async def ranking_show(self, group_id: str, user_id: str, nickname: str) -> dict:
        data = await self.get_player(group_id, user_id, nickname)
        if not data["slave"]:
            return notice(
                "🚫", "你还没有奴隶，无法查看排位赛信息", [], tone="warn"
            )
        # 一次性把全部奴隶的排行信息查回来：避免 N 次 self.db.load 的
        # SQLite 开/闭开销；用 list 转 dict 也减少下游查找的 O(n^2)。
        slave_ids = [str(s) for s in data["slave"]]
        rows = await self.db.query_players_by_uids(group_id, slave_ids)
        slaves_by_id = {uid: p for uid, p in rows}
        rows = []
        for sid in slave_ids:
            slave = slaves_by_id.get(sid)
            if slave is None:
                # 期间被删档：跳过；之前会 KeyError 整条指令崩
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
        text = "【奴隶排位赛信息】\n" + "\n".join(
            f"{r['name']}：段位 {r['tier']}｜分数 {r['score']}｜场次 {r['matches']}"
            for r in rows
        )
        text += (
            "\n【段位说明】青铜 <1000｜白银 <1400｜黄金 <1800｜铂金 <2200｜钻石 ≥2200"
        )
        return R(tmpl="rank_match", data={"info_mode": True, "rows": rows}, text=text)

    # ================= 银行 =================

    def _rate_cfg(self) -> tuple[float, int]:
        rate = self._num("bank", "interestRate", default=0.01, lo=0.0, hi=0.5)
        max_hours = self._int("bank", "maxInterestTime", default=24, lo=0, hi=720)
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
            # 不再用 int() 截断：金币是浮点，整截会让 0.7 之类的尾数被静默吞掉
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
        price_multi = self._num("bank", "upgradePriceMulti", default=1.2, lo=1.01, hi=100.0)
        limit_multi = self._num("bank", "limitIncreaseMulti", default=1.25, lo=1.0, hi=10.0)

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
        min_amount = self._int("transfer", "minAmount", default=100, lo=1, hi=10**12)
        fee_rate = self._num("transfer", "feeRate", default=0.1, lo=0.0, hi=1.0)
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

    async def my_slave(self, group_id: str, user_id: str, nickname: str) -> dict:
        data = await self.get_player(group_id, user_id, nickname)
        master_name = ""
        if data["master"]:
            master_name = await self.name_of(group_id, data["master"])
        # 与 ranking_show 同源改造：N+1 → 1 次 SELECT IN (..)
        slave_ids = [str(s) for s in data["slave"]]
        slaves_by_id = {
            uid: p for uid, p in await self.db.query_players_by_uids(group_id, slave_ids)
        }
        slaves = []
        for sid in slave_ids:
            slave = slaves_by_id.get(sid)
            if slave is None:
                continue
            slaves.append(
                {
                    "id": sid,
                    "name": self._name(slave, sid),
                    "value": _fmt(slave["value"]),
                }
            )
        info = {
            "nickname": data.get("nickname") or nickname or f"用户{user_id}",
            "uid": str(user_id),
            "currency": _fmt(data["currency"]),
            "value": _fmt(data["value"]),
            "slave_count": len(slaves),
            "master": master_name or "无",
            "slaves": slaves[:MARKET_LIMIT],
            "slaves_truncated": len(slaves) > MARKET_LIMIT,
            "wins": data["battleStats"]["wins"],
            "losses": data["battleStats"]["losses"],
        }
        text = (
            f"# {info['nickname']} 的基础信息\n"
            f"金币：{info['currency']}｜身价：{info['value']}\n"
            f"拥有奴隶：{info['slave_count']} 个｜主人：{info['master']}\n"
            f"决斗战绩：{info['wins']} 胜 {info['losses']} 负"
        )
        if slaves:
            text += "\n奴隶列表：\n" + "\n".join(
                f"• {s['name']}（{s['id']}）身价 {s['value']}" for s in info["slaves"]
            )
        return R(tmpl="myslave", data=info, text=text)

    async def market_list(self, group_id: str) -> dict:
        # 一条 SQL 按身价倒序取前 N，不再"列 id 再逐个 load"
        rows = await self.db.query_players(
            group_id, order_by="value", desc=True, limit=MARKET_LIMIT
        )
        total = await self.db.count_players(group_id)
        names: dict[str, str] = {uid: self._name(p, uid) for uid, p in rows}
        # 主人昵称一次性批量补齐：避免主人在 top 100 之外时 market 列表里
        # 每条都 await self.name_of() 单次开/关 SQLite 连接，N+1 让指令慢 1~3s
        miss_ids = [
            mid for _, p in rows
            for mid in [p.get("master") or ""]
            if mid and mid not in names
        ]
        if miss_ids:
            extra = await self.db.query_players_by_uids(group_id, list(dict.fromkeys(miss_ids)))
            for uid, p in extra:
                names[uid] = self._name(p, uid)
        items = []
        for uid, p in rows:
            master_id = p.get("master") or ""
            if master_id:
                master_name = names.get(master_id) or "无"
            else:
                master_name = "无"
            items.append(
                {
                    "id": uid,
                    "name": names[uid],
                    "value": _fmt(p["value"]),
                    "master": master_name,
                }
            )
        text = f"🛒 奴隶市场（共 {total} 人）\n" + "\n".join(
            f"{i}. {it['name']}（{it['id']}）身价 {it['value']}｜主人：{it['master']}"
            for i, it in enumerate(items, 1)
        )
        return R(tmpl="market", data={"items": items, "total": total}, text=text)

    # 排行榜类型 -> (排序列, 展示标题)
    _BOARDS: ClassVar[dict[str, tuple[str, str]]] = {
        "currency": ("currency", "金币排行榜"),
        "value": ("value", "身价排行榜"),
        # slave 榜展示键是 JSON 数组长度、bank 榜是 (level, balance) 二元组，
        # 单一 SQL 列都无法正确预截断，只能全量取回后内存排序
        "slave": ("uid", "奴隶数量排行榜"),
        "bank": ("bank_level", "银行等级排行榜"),
    }

    async def leaderboard(self, group_id: str, kind: str) -> dict:
        """kind: currency / value / slave / bank"""
        if kind not in self._BOARDS:
            kind = "currency"
        col, title = self._BOARDS[kind]
        if kind in ("slave", "bank"):
            # 全量扫描：按 uid/level 预截断会采到错误样本——uid 倒序前 N 与
            # "奴隶最多"无关，level 前 15 会挤掉同级但余额更高的玩家。
            # 行数用 FULL_SCAN_CAP 兜底异常膨胀的库。
            rows = await self.db.query_players(
                group_id, order_by=col, desc=True, limit=FULL_SCAN_CAP
            )
        else:
            # currency/value 的展示键就是排序列本身：SQL 直接取前 15 名即可，
            # 并列名次谁进榜都一样，无需全量扫描
            rows = await self.db.query_players(
                group_id, order_by=col, desc=True, limit=BOARD_LIMIT
            )
        entries = [
            {
                "id": uid,
                "name": self._name(p, uid),
                "currency": p["currency"],
                "value": p["value"],
                "slave_count": len(p["slave"]),
                "bank_level": p["bank"]["level"],
                "bank_balance": p["bank"]["balance"],
            }
            for uid, p in rows
        ]
        keymap = {
            "currency": lambda e: e["currency"],
            "value": lambda e: e["value"],
            "slave": lambda e: e["slave_count"],
            "bank": lambda e: (e["bank_level"], e["bank_balance"]),
        }
        entries.sort(key=keymap[kind], reverse=True)
        entries = entries[:BOARD_LIMIT]

        def _score(e: dict) -> str:
            if kind == "currency":
                return f"{_fmt(e['currency'])} 💰"
            if kind == "value":
                return f"{_fmt(e['value'])} 💎"
            if kind == "slave":
                return f"{e['slave_count']} 个"
            return f"Lv.{e['bank_level']}（{_fmt(e['bank_balance'])}）"

        board = [
            {"rank": i + 1, "name": e["name"], "id": e["id"], "score": _score(e)}
            for i, e in enumerate(entries)
        ]
        text = f"🏆 {title}（前 {len(board)} 名）\n" + "\n".join(
            f"{r['rank']}. {r['name']}（{r['id']}）- {r['score']}" for r in board
        )
        return R(tmpl="ranking", data={"title": title, "rows": board}, text=text)

    # ================= 备份 =================

    async def create_backup(self) -> dict:
        name = await self.db.create_backup()
        return notice(
            "💾",
            "备份创建成功！",
            [f"备份文件：{name}", "可使用「奴隶备份列表」查看所有备份"],
        )

    async def list_backups(self) -> dict:
        backups = await self.db.list_backups()
        if not backups:
            return notice("📭", "当前没有任何备份", [], tone="warn")
        return notice(
            "🗄️",
            "备份列表",
            [f"{i + 1}. {b}" for i, b in enumerate(backups)]
            + ["可使用「奴隶恢复备份 序号」恢复"],
        )

    async def restore_backup(self, index: int) -> dict:
        try:
            name = await self.db.restore_backup(index)
        except ValueError as e:  # 备份文件损坏，已在 db 层拒绝覆盖主库
            return notice("🚫", "恢复失败", [str(e)], tone="err")
        if name is None:
            return notice("🚫", "无效的备份序号", [], tone="err")
        return notice("✅", "备份恢复成功！", [f"恢复时间点：{name}"])

    async def delete_backup(self, index: int) -> dict:
        name = await self.db.delete_backup(index)
        if name is None:
            return notice("🚫", "无效的备份序号", [], tone="err")
        return notice("🗑️", "备份删除成功！", [f"已删除：{name}"])

    # ================= WebUI 数据接口 =================

    async def stats(self) -> dict:
        """全局统计（WebUI 总览用）：单条聚合 SQL，不再逐个玩家 load。"""
        return await self.db.totals()

    async def group_counts(self) -> list[dict]:
        return await self.db.group_counts()

    def profile_of(self, gid: str, uid: str, p: dict) -> dict:
        return {
            "gid": str(gid),
            "uid": str(uid),
            "nickname": p.get("nickname") or f"用户{uid}",
            "currency": round(p["currency"], 2),
            "value": round(p["value"], 2),
            "master": p.get("master") or "",
            "slave_count": len(p["slave"]),
            "bank_level": p["bank"]["level"],
            "bank_balance": round(p["bank"]["balance"], 2),
            "bank_limit": p["bank"]["limit"],
            "tier": p["ranking"]["tier"],
            "rank_score": p["ranking"]["score"],
            "wins": p["battleStats"]["wins"],
            "losses": p["battleStats"]["losses"],
        }

    async def page_profiles(
        self, gid: str, page: int = 1, size: int = 20
    ) -> tuple[int, list[dict]]:
        """分页取玩家档案 -> (总数, 本页档案)。WebUI 玩家列表用。"""
        page = max(1, int(page))
        size = min(200, max(1, int(size)))
        total = await self.db.count_players(gid)
        rows = await self.db.query_players(
            gid, order_by="uid", limit=size, offset=(page - 1) * size
        )
        return total, [self.profile_of(gid, uid, p) for uid, p in rows]

    async def all_profiles(self, gid: str) -> list[dict]:
        rows = await self.db.query_players(gid, order_by="uid")
        return [self.profile_of(gid, uid, p) for uid, p in rows]
