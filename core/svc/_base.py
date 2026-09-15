"""GameService 的基类 Mixin：配置读取、冷却、玩家访问与主奴关系工具。

`_num` / `_int` / `_cd_left` / `_name` / `_owns` 等工具都在这里，
各域 Mixin（打工/购买/抢劫/训练/决斗/排位/银行/榜单/备份/WebUI 接口）
通过组合共享它们。
"""

from __future__ import annotations

import math
import random

from ._const import _schema_meta, ui_text


class _BaseMixin:
    # ================= 基础工具 =================

    def t(self, key: str, default: str, **vars: object) -> str:
        """取一条用户可见文案（uiTexts 表，缺失回落代码内置 default）。

        svc 层全部面向用户的回复都从这里取文案；default 必须与
        resources/data/uiTexts.json 里的值逐字一致（lint 测试核对）。
        """
        return ui_text(key, default, **vars)

    def _meta(self, *path) -> dict:
        """按点分路径取 schema 元数据；缺失时返回空 dict。"""
        return _schema_meta().get(".".join(str(p) for p in path), {})

    def _cfg(self, *path, default=None):
        node = self.config
        for p in path:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        if node == {}:
            # 未配置或值为空 dict：回退 schema 里的 default（唯一事实来源）
            meta = self._meta(*path)
            return default if default is not None else meta.get("default", {})
        return node

    def _num(self, *path, default=None, lo=None, hi=None) -> float:
        """读浮点配置并夹到 [lo, hi]：配置写错（如概率填 5）不该变成必中/无限刷。

        default/lo/hi 均可省略——缺省时从 _conf_schema.json 取，schema 是唯一
        事实来源；显式传入用于个别需要覆盖区间上限的调用（如排行榜）。
        """
        meta = self._meta(*path)
        if default is None:
            default = meta.get("default")
        if lo is None:
            lo = meta.get("min")
        if hi is None:
            hi = meta.get("max")
        if default is None:
            default = 0.0
        if lo is None:
            lo = -float("inf")
        if hi is None:
            hi = float("inf")
        try:
            v = float(self._cfg(*path, default=default))
        except (TypeError, ValueError):
            v = float(default)
        if not math.isfinite(v):
            v = float(default)
        return min(hi, max(lo, v))

    def _int(self, *path, default: int | None = None, lo: int | None = None, hi: int | None = None) -> int:
        return int(self._num(*path, default=default, lo=lo, hi=hi))

    @staticmethod
    def _rand(lo: int, hi: int) -> int:
        """闭区间随机整数。配置把上下限填反时自动交换，不让 randint 抛异常。"""
        return random.randint(min(lo, hi), max(lo, hi))

    def _no_cd(self, user_id: str) -> bool:
        return str(user_id) in {
            str(u) for u in self.config.get("ignoreCDUsers", []) or []
        }

    def _cd_left(self, data: dict, key: str, cd: int, user_id: str, now: int) -> int:
        """统一冷却计算。返回剩余秒数（0 表示可以执行）。

        系统时钟回拨（容器时间同步、跨机迁移）会让存档里的时间戳大于当前时间，
        这里把"未来的时间戳"直接当作 0 处理，避免把玩家锁死。
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
        """只读取存档。昵称更新走 set_card（只 UPDATE nickname 列），
        避免整行回写覆盖并发改动。"""
        data = await self.db.load(group_id, user_id)
        if nickname and nickname != data.get("nickname", ""):
            await self.db.set_card(group_id, user_id, nickname)
            data["nickname"] = nickname
        return data

    async def name_of(self, group_id: str, user_id: str) -> str:
        data = await self.db.load(group_id, user_id)
        return data.get("nickname") or self.t("ui_unknown_user", "用户{uid}", uid=user_id)

    def _name(self, data: dict, uid: str) -> str:
        return data.get("nickname") or self.t("ui_unknown_user", "用户{uid}", uid=uid)

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
