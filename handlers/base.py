"""声明式路由基座。

AstrBot 通过 handler.__module__ 与插件主模块做【精确匹配】来绑定插件实例
（见 star_handler.get_handlers_by_module_name），因此所有被装饰的函数
必须归属到主模块。install() 在装饰前重写 __module__，从而允许把路由表
安全地拆分到任意子模块中。

注意：此写法依赖 AstrBot 内部的 get_handlers_by_module_name 精确匹配行为，
升级 AstrBot 后若指令全部失效，优先检查这里。
"""

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..core.result import notice

GID_HINT = "该功能只能在群聊中使用"


def _fallback_logger():
    # 旧内核的 Star 可能没有 self.logger：退到 AstrBot 的公共 logger 代理
    try:
        from astrbot.api import logger as _logger

        return _logger
    except Exception:  # noqa: BLE001 - 极端情况下退到标准库
        import logging

        return logging.getLogger("astrbot")


MAX_ARG_LEN = 32  # 数字参数最大位数：防止超长串进 int() 抛 ValueError

# 每用户指令锁：同一 (群, 用户) 的指令串行执行，
# 消除「读余额→判断→写回」式资金操作被连发指令并发击穿的双花窗口。
#
# 锁表为 GameCtx 实例字段：跟 plugin 生命周期走，terminate() 清空。
# 正在持有的锁要等协程释放，那是指令路径里的正常等锁，与卸载互不干扰：
# 卸载后没有任何 handler 还在 await acquire()，原本的 _lock.locked()=
# True 在协程退出后自动 False，下次 acquire 拿到新表里的全新锁。
_LOCK_CAP = 4096


class PlayerLockTable:
    """每 (群, 用户) 一把 asyncio.Lock，串行化单用户的多指令并发。"""

    def __init__(self, cap: int = _LOCK_CAP):
        self._cap = max(64, int(cap))
        self._locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()

    def acquire(self, gid, uid) -> asyncio.Lock:
        key = (str(gid or ""), str(uid))
        lock = self._locks.get(key)
        if lock is not None:
            self._locks.move_to_end(key)
            return lock
        if len(self._locks) >= self._cap:
            for k in [k for k, v in self._locks.items() if not v.locked()][
                : self._cap // 4
            ]:
                self._locks.pop(k, None)
        lock = self._locks.setdefault(key, asyncio.Lock())
        return lock

    def clear(self) -> None:
        """热重载 / 卸载时清空锁表。不抢持锁中锁，让它们按协程退出自然释放。"""
        self._locks.clear()


@dataclass
class Route:
    pattern: str
    name: str
    doc: str
    run: Callable[..., Awaitable]
    admin: bool = False
    group_only: bool = True  # 群聊限定：由 install() 统一前置校验
    priority: int = 0


def install(cls, flt, module_path: str, routes) -> int:
    """把路由安装到插件类上，返回安装数量。

    flt 为 astrbot.api.event.filter 子模块。
    run() 是普通协程返回 R(dict) / None；None 表示静默不回复。
    统一职责：群聊限定校验、每用户串行锁、
    异常兜底（内部错误转提示消息，绝不让指令无声失败）、输出渲染。

    锁表为插件实例字段（`cls._player_locks`），卸载 / 热重载
    时由 main.terminate() 调 PlayerLockTable.clear() 清空。
    """
    installed = 0
    for route in routes:

        async def handler(self, event, _route=route):
            # 锁表 lazy-init：第一次跑指令时拿到实例字段。
            # 走 self._player_locks 而不是类属性，方便子插件 / 测试用 stub 替换。
            locks = getattr(self, "_player_locks", None)
            if locks is None:
                locks = PlayerLockTable()
                self._player_locks = locks
            gid = gid_of(event)
            if _route.group_only and not gid:
                event.stop_event()
                yield event.plain_result(GID_HINT)
                return
            # refresh_card 自身已带超时并吞掉所有异常，这里无需再包一层
            await self.ctx.refresh_card(event)
            async with locks.acquire(gid, event.get_sender_id()):
                try:
                    r = await _route.run(self.ctx, event)
                except Exception:  # noqa: BLE001 - 内部错误兜底，不中断指令
                    # logger 兜底：旧内核的 Star 上可能没有 self.logger，
                    # 兜底逻辑本身再抛 AttributeError 会让指令静默失败
                    log = getattr(self, "logger", None) or _fallback_logger()
                    log.exception("[slave_market] 处理指令时出错")
                    r = notice("⚠️", "处理请求时出错，请稍后再试。", [], tone="err")
            if r is None:
                return
            event.stop_event()
            if r.get("err"):
                yield event.plain_result(str(r["err"])[:500])
                return
            if r.get("img"):  # 直接给图片路径（不经模板渲染）
                yield event.image_result(str(r["img"]))
                return
            img = await self.ctx.render(r.get("tmpl"), r.get("data") or {})
            if img:
                yield event.image_result(img)
            else:
                text = str(r.get("text") or "").strip()
                yield event.plain_result(text[:1800] if text else "（执行完成）")

        handler.__name__ = route.name
        handler.__qualname__ = f"{cls.__name__}.{route.name}"
        handler.__doc__ = route.doc
        handler.__module__ = module_path

        if route.admin:
            handler = flt.permission_type(flt.PermissionType.ADMIN)(handler)
        handler = flt.regex(route.pattern, priority=route.priority)(handler)
        setattr(cls, route.name, handler)
        installed += 1
    return installed


# ================= 事件解析辅助（handlers 共用） =================


def gid_of(event) -> str:
    return str(event.get_group_id() or "")


def uid_of(event) -> str:
    return str(event.get_sender_id())


def nickname_of(event) -> str:
    return str(event.get_sender_name() or "")


def is_admin(event) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:  # noqa: BLE001
        return False


def at_targets(event) -> list[str]:
    """消息中的 @ 目标（过滤机器人自身与全体@）。

    不做 isdigit 校验：QQ 官方平台的 uid 是含字母的 openid，
    isdigit 会把所有 @ 目标全部滤掉。
    """
    import astrbot.api.message_components as Comp

    self_id = str(event.get_self_id())
    out = []
    for c in event.message_obj.message:
        if not isinstance(c, Comp.At):
            continue
        qq = str(c.qq or "").strip()
        if not qq or qq == self_id or qq.lower() == "all":
            continue
        out.append(qq)
    return out


def numbers(event) -> list[str]:
    """消息里的数字参数（过长的串直接丢弃，避免 int() 抛异常）。"""
    return [
        n for n in re.findall(r"\d+", event.message_str or "") if len(n) <= MAX_ARG_LEN
    ]


def target_of(event) -> str | None:
    """购买/训练/放生等指令的目标：优先 @，其次数字参数。机器人自身被排除。"""
    self_id = str(event.get_self_id())
    ats = at_targets(event)
    if ats:
        return ats[0]
    for n in numbers(event):
        if n != self_id:
            return n
    return None
