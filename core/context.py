"""GameCtx：handlers 与 main/webui 之间共享的轻量上下文。

含平台昵称拉取（参考 astrbot_plugin_shangbanzu 的适配思路）：
- aiocqhttp / OneBot：bot.api.call_action 拉群名片
- QQ 官方：botpy 的 Route HTTP 接口拉群名/成员昵称（灰度接口，未开放时负缓存）
所有昵称带容量受控的内存缓存并入库，模板与排行榜即可显示真实昵称。
"""

from __future__ import annotations

import asyncio
import time as _time
from collections import OrderedDict
from pathlib import Path

import astrbot.api.message_components as Comp

try:  # 兼容包加载与 sys.path 加载两种方式
    from .db import PlayerDB
    from .renderer import PlaywrightRenderer
    from .service import GameService
    from .texts import Texts
except ImportError:  # pragma: no cover
    from db import PlayerDB  # type: ignore
    from renderer import PlaywrightRenderer  # type: ignore
    from service import GameService  # type: ignore
    from texts import Texts  # type: ignore

VERSION = "1.0.0"

# 昵称缓存：条目 (expire_ts, card)；容量超限先清过期再清最旧
_CARD_CACHE_CAP = 2000
_CARD_TTL = 600  # 命中有效期（秒）
_CARD_NEG_TTL = 300  # 未命中负缓存（秒）
_CARD_FETCH_TIMEOUT = 5  # 昵称拉取整体超时（秒）：平台无响应时不挂住指令


class GameCtx:
    def __init__(
        self, plugin, config: dict, data_root: Path, copywriting: dict, logger
    ):
        # plugin 参数保留兼容旧调用方（main.py 以 GameCtx(self, ...) 构造）；
        # 插件实例仅经 self.service 依赖注入 db/renderer，本类不再持有该引用。
        self.config = config
        self.log = logger
        self.copy = copywriting
        self.data_root = Path(data_root)
        # SQLite 存档库；schema 初始化在 initialize() 中 await ctx.db.init() 完成
        self.db = PlayerDB(
            self.data_root / "slave_market.db",
            backup_keep=int(config.get("backupKeep", 10)),
            bank_init=config.get("bank") or {},
        )
        self.service = GameService(self.db, config, copywriting)
        self.renderer = PlaywrightRenderer(
            self.data_root / "screenshots",
            scale=float(config.get("render_scale", 2.0) or 2.0),
            logger=logger,
        )
        self.texts = Texts(
            Path(__file__).resolve().parent.parent / "resources" / "texts"
        )
        self._card_cache: "OrderedDict[tuple[str, str], tuple[float, str]]" = (
            OrderedDict()
        )
        self._tmpl_text: dict[str, str] = {}  # 模板文件内容缓存（避免每次出图读盘）

    def set_copywriting(self, copy: dict) -> None:
        """设置/热更新游戏文案（同步更新 ctx 与 service 的引用）。"""
        self.copy = copy
        self.service.copy = copy

    def reload_texts(self) -> None:
        """热更新长文本（帮助等），由 WebUI 保存后调用。"""
        self.texts.load_all(force=True)

    # ---------- 平台昵称拉取 ----------

    @staticmethod
    def _collect_uids(event, extra_uids=()) -> list[str]:
        uids = [str(event.get_sender_id())]
        for comp in event.get_messages() if hasattr(event, "get_messages") else []:
            if getattr(comp, "type", "") == "At" or isinstance(comp, Comp.At):
                qq = str(getattr(comp, "qq", ""))
                if qq and qq != "all" and qq != str(event.get_self_id()):
                    uids.append(qq)
        for uid in extra_uids or ():
            if uid:
                uids.append(str(uid))
        seen: set[str] = set()
        out = []
        for u in uids:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _cache_set(self, key, expire_ts, val, now=None) -> None:
        now = _time.time() if now is None else now
        if len(self._card_cache) >= _CARD_CACHE_CAP:
            for k in [k for k, v in self._card_cache.items() if v[0] <= now]:
                self._card_cache.pop(k, None)
            while len(self._card_cache) > _CARD_CACHE_CAP:
                self._card_cache.popitem(last=False)  # LRU：淘汰最久未被刷新的
        self._card_cache[key] = (expire_ts, val)
        self._card_cache.move_to_end(key)

    async def refresh_card(self, event, extra_uids=()) -> None:
        """拉取发送者与 @ 目标的群名片/昵称；任何失败静默，不影响指令。

        整体带超时：平台网关无响应时不能把用户的指令协程一直挂住。
        """
        try:
            await asyncio.wait_for(
                self._refresh_card(event, extra_uids), timeout=_CARD_FETCH_TIMEOUT
            )
        except (TimeoutError, asyncio.TimeoutError):
            self.log.debug("[奴隶市场] 昵称拉取超时，已跳过")
        except Exception:  # noqa: BLE001 - 昵称只是显示优化，绝不影响指令
            pass

    async def _refresh_card(self, event, extra_uids=()) -> None:
        gid = event.get_group_id()
        if not gid:
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        api = getattr(bot, "api", None)
        if hasattr(api, "call_action"):
            await self._refresh_card_onebot(gid, api, event, extra_uids)
            return
        http = getattr(getattr(bot, "api", None), "_http", None) or getattr(
            bot, "_http", None
        )
        if http is not None:
            await self._refresh_card_qqofficial(gid, http, event, extra_uids)

    def _pending_uids(self, gid, event, extra_uids, now) -> list[str]:
        """过滤掉缓存仍然有效的 uid（含负缓存）。"""
        out = []
        for uid in self._collect_uids(event, extra_uids):
            hit = self._card_cache.get((str(gid), uid))
            if hit and hit[0] > now:
                continue
            out.append(uid)
        return out

    async def _refresh_card_onebot(self, gid, api, event, extra_uids) -> None:
        now = _time.time()
        uids = self._pending_uids(gid, event, extra_uids, now)
        if not uids:
            return

        async def _one(uid: str) -> tuple[str, str]:
            try:
                info = await api.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=False,
                )
                return uid, str(info.get("card") or info.get("nickname") or "").strip()
            except Exception:  # noqa: BLE001 - 单人失败不影响其他人
                return uid, ""

        # 并发拉取：串行时 N 个 @ 目标会把等待时间乘 N
        for uid, card in await asyncio.gather(*(_one(u) for u in uids)):
            await self._store_card(gid, uid, card, now)

    async def _refresh_card_qqofficial(self, gid, http, event, extra_uids) -> None:
        """QQ 官方平台：botpy 的 BotHttp 自动管理 access_token。

        - 成员昵称：探测灰度接口 /v2/groups/{g}/members/{openid}，未开放时负缓存
        """
        try:
            from botpy.http import Route
        except ImportError:  # 非 official 安装不含 botpy
            return
        now = _time.time()
        uids = self._pending_uids(gid, event, extra_uids, now)
        if not uids:
            return

        async def _one(uid: str) -> tuple[str, str]:
            card = ""
            try:
                route = Route(
                    "GET",
                    "/v2/groups/{group_openid}/members/{member_openid}",
                    group_openid=str(gid),
                    member_openid=uid,
                )
                info = await http.request(route)
                if isinstance(info, dict) and info:
                    user = info.get("user") or {}
                    if not user.get("id") or str(user.get("id")) == uid:
                        card = str(
                            info.get("nick")
                            or info.get("nickname")
                            or info.get("card")
                            or user.get("username")
                            or user.get("nickname")
                            or ""
                        ).strip()
            except Exception:  # noqa: BLE001 - 灰度接口未开放时静默
                pass
            return uid, card

        # 并发拉取：串行时 N 个 @ 目标会把等待时间乘 N（总超时只有几秒）
        for uid, card in await asyncio.gather(*(_one(u) for u in uids)):
            await self._store_card(gid, uid, card, now)

    async def _store_card(self, gid, uid, card, now) -> None:
        key = (str(gid), str(uid))
        if card:
            self._cache_set(key, now + _CARD_TTL, card, now)
            await self.db.set_card(str(gid), str(uid), card)
        else:
            self._cache_set(key, now + _CARD_NEG_TTL, "", now)

    # ---------- 模板渲染管线 ----------

    def template_path(self, name: str) -> Path:
        return (
            Path(__file__).resolve().parent.parent
            / "resources"
            / "templates"
            / f"{name}.html"
        )

    async def render(self, tmpl: str | None, data: dict) -> str | None:
        """渲染模板并截图；失败返回 None（上层回退 text）。"""
        if not tmpl or not bool(self.config.get("use_image", True)):
            return None
        try:
            data = dict(data or {})
            data.setdefault("plugin_version", VERSION)
            tmpl_str = self._tmpl_text.get(tmpl)
            if tmpl_str is None:  # 模板内容随插件发布固定，只读一次
                tmpl_str = await asyncio.to_thread(
                    self.template_path(tmpl).read_text, "utf-8"
                )
                self._tmpl_text[tmpl] = tmpl_str
            html = await asyncio.to_thread(
                self.renderer.render_template, tmpl_str, data
            )
            return await self.renderer.screenshot(html, name=tmpl)
        except Exception as e:  # noqa: BLE001 - 渲染失败必须回退文本而非中断指令
            self.log.warning(f"[奴隶市场] 渲染失败回退文本：{e}")
            return None
