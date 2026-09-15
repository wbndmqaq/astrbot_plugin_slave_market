"""GameCtx：handlers 与 main/webui 之间共享的轻量上下文。

含平台昵称拉取（参考 astrbot_plugin_shangbanzu 的适配思路）：
- aiocqhttp / OneBot：bot.api.call_action 拉群名片
- QQ 官方：botpy 的 Route HTTP 接口拉群名/成员昵称（灰度接口，未开放时负缓存）
所有昵称带容量受控的内存缓存并入库，模板与排行榜即可显示真实昵称。
"""

from __future__ import annotations

import asyncio
import math
import time as _time
from collections import OrderedDict
from pathlib import Path

import astrbot.api.message_components as Comp

from .db import PlayerDB
from .renderer import PlaywrightRenderer
from .service import GameService
from .svc import set_ui_texts
from .texts import Texts

VERSION = "1.0.1"

# 昵称缓存：条目 (expire_ts, card)；容量超限先清过期再清最旧
_CARD_CACHE_CAP = 2000
_CARD_TTL = 600  # 命中有效期（秒）
_CARD_NEG_TTL = 300  # 未命中负缓存（秒）
_CARD_FETCH_TIMEOUT = 5  # 昵称拉取整体超时（秒）：平台无响应时不挂住指令


def _cfg_int(config, key: str, default: int, lo: int, hi: int) -> int:
    """读整型配置并夹到 [lo, hi]；脏值（""/None/"abc"/inf）回落 default。

    GameCtx 是在 SlaveMarket.__init__ 里构造的，这里抛异常等于整个插件
    （含全部游戏指令）加载失败。配置文件是用户可手改的，必须容错。
    """
    try:
        v = int(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, v))


def _cfg_float(config, key: str, default: float, lo: float, hi: float) -> float:
    """读浮点配置并夹到 [lo, hi]；脏值/NaN/inf 回落 default。"""
    try:
        v = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


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
            backup_keep=_cfg_int(config, "backupKeep", 10, 0, 1000),
            bank_init=config.get("bank") or {},
        )
        self.service = GameService(self.db, config, copywriting)
        set_ui_texts(copywriting)
        self._sync_copy_to_storage()
        self.renderer = PlaywrightRenderer(
            self.data_root / "screenshots",
            scale=_cfg_float(config, "render_scale", 2.0, 1.0, 4.0),
            logger=logger,
        )
        # 长文本：内置 resources/texts（只读）+ data_root/overrides/texts（WebUI 可写）
        self.texts = Texts(self.data_root)
        self._card_cache: OrderedDict[tuple[str, str], tuple[float, str]] = (
            OrderedDict()
        )
        self._tmpl_text: dict[str, str] = {}  # 模板文件内容缓存（避免每次出图读盘）

    def _sync_copy_to_storage(self) -> None:
        """把文案里影响「建号默认值」的部分同步给存储层。

        目前只有 gameTexts 的段位表首档（新号的初始分数/段位）。文案是唯一
        事实来源：用户在 WebUI 把段位表改成 500 起步后，建号必须是 499/首档名
        （门槛是「离开该档」的上界），否则库里写死的默认值会漏到界面，
        打一场就跳到别的档位。
        """
        self.db.set_rank_init((self.copy or {}).get("ranking_tiers"))

    def set_copywriting(self, copy: dict) -> None:
        """设置/热更新游戏文案（同步更新 ctx 与 service 的引用 + 建号默认值）。"""
        self.copy = copy
        self.service.copy = copy
        # uiTexts（交互回复/模板文案）注入 svc 层的模块级查询表：
        # handlers 的用法提示与 _cd_text 的单位词没有 self.copy 可走，
        # 统一经 ui_text() 读这张表（缺失时回落代码内置默认值）
        set_ui_texts(copy)
        self._sync_copy_to_storage()

    def t(self, key: str, default: str, **vars: object) -> str:
        """handlers 用：取一条用户可见文案（uiTexts 表，缺失回落 default）。"""
        from .svc._const import ui_text

        return ui_text(key, default, **vars)

    def template_texts(self) -> dict:
        """模板静态文案（uiTexts 里 tpl_ 前缀的键），注入 Jinja2 的 `t`。

        模板里写 {{ t.键名 }}，键名不带 tpl_ 前缀；键缺失时模板显示空 ——
        uiTexts.json 与代码内置值随插件分发，正常情况不会缺。
        """
        return {
            k[4:]: v
            for k, v in (self.copy or {}).items()
            if k.startswith("tpl_") and isinstance(v, str)
        }

    def reload_texts(self, force: bool = True) -> None:
        """热更新长文本（帮助等），由 WebUI 保存后调用。

        保存路径在 data_root/overrides/ 下，因此这里重新合并「内置 + 用户覆盖」。
        """
        self.texts.load_all(force=force)

    # ---------- 平台昵称拉取 ----------

    @staticmethod
    def _collect_uids(event, extra_uids=()) -> list[str]:
        uids = [str(event.get_sender_id())]
        for comp in event.get_messages() if hasattr(event, "get_messages") else []:
            if getattr(comp, "type", "") == "At" or isinstance(comp, Comp.At):
                qq = str(getattr(comp, "qq", ""))
                if qq and qq != "all" and qq != str(event.get_self_id()):
                    uids.append(qq)
        uids.extend(str(u) for u in extra_uids or () if u)
        # 去重且保序：dict.fromkeys 就是"按首次出现顺序去重"
        return list(dict.fromkeys(uids))

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
        except TimeoutError:
            self.log.debug("[奴隶市场] 昵称拉取超时，已跳过")
        except Exception:  # noqa: BLE001, S110 - 昵称只是显示优化，绝不影响指令
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
            except Exception:  # noqa: BLE001, S110 - 灰度接口未开放时静默
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
            # 模板静态文案统一注入：10 套模板里的标题/字段名/兜底值都从这里取，
            # 免得 HTML 成为文案外置的最后一个例外
            data.setdefault("t", self.template_texts())
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
