"""astrbot_plugin_slave_market —— 奴隶市场（模块化主入口）。

架构：
    main.py            仅保留插件生命周期 + 输出渲染，指令通过声明式路由安装
    handlers/          指令路由表（按域拆分：市场/社交/战斗/银行/系统）
    core/              业务服务层 + SQLite 存储 + 独立 Playwright 渲染器 + 长文本加载
    webui/             独立端口 WebUI 面板（aiohttp）
    resources/         游戏文案 JSON / HTML 渲染模板

依赖：
    playwright（需执行一次 python -m playwright install chromium）
    aiohttp、jinja2

说明：
    AstrBot 以 handler.__module__ 与插件主模块做【精确匹配】来绑定插件实例
    （star_handler.get_handlers_by_module_name），因此 handlers/ 中的路由函数
    在装饰前由 install() 将 __module__ 重写为本模块路径。
"""

import asyncio
import secrets
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

try:
    from .core.auth import (
        Argon2Hasher,
        AuthError,
        AuthUnavailable,
        PasswordStore,
        rotate_password,
    )
    from .core.context import VERSION, GameCtx
    from .handlers import ALL_ROUTES, install
    from .webui.server import TEMP_PASSWORD_FILE, WebUIServer, _is_loopback_host
except ImportError:  # 兼容以文件方式直接加载的旧版内核
    import sys

    # 顶层名 core/handlers/webui 是兜底导入的通用名：无论是其它插件的兜底
    # 残留还是本插件上次热重载的旧模块，都必须先清出 sys.modules，否则下面
    # 拿到的是别人的/过期的代码（内核重载只清 data.plugins.* 前缀）。
    for _name in ("core", "handlers", "webui"):
        for _key in [k for k in sys.modules if k == _name or k.startswith(_name + ".")]:
            del sys.modules[_key]
    sys.path.insert(0, str(Path(__file__).parent))
    from core.auth import (
        Argon2Hasher,
        AuthError,
        AuthUnavailable,
        PasswordStore,
        rotate_password,
    )
    from core.context import VERSION, GameCtx
    from handlers import ALL_ROUTES, install
    from webui.server import TEMP_PASSWORD_FILE, WebUIServer, _is_loopback_host

PLUGIN_NAME = "astrbot_plugin_slave_market"
# 临时密码文件名的唯一来源在 webui/server.py（TEMP_PASSWORD_FILE），此处不再重复定义


def _is_loopback(host: str) -> bool:
    """host 是否只对本机可见（委托给 server 模块的统一实现）。"""
    return _is_loopback_host(host)


class SlaveMarket(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        # 文案在 initialize() 中异步加载；__init__ 不做磁盘 IO
        self.ctx = GameCtx(self, self.config, self._data_dir(), {}, logger)
        self._webui = None
        # 指令路由的进程级锁表迁到实例字段：terminate() 清空，避免
        # 热重载后跨 loop 残留的 asyncio.Lock 引发 double-spend 窗口
        self._player_locks = None  # 由 install/handler lazy-init

    async def initialize(self):
        try:
            self.ctx.set_copywriting(await asyncio.to_thread(self._load_copywriting))
            await asyncio.to_thread(self.ctx.texts.load_all)
            await self.ctx.db.init()
            # WebUI 启用的判定延后到密码状态确认之后：临时密码生成
            # 仅在用户没设过密码时发生。
            webui_on = bool((self.config or {}).get("webui_enabled", True))
            if webui_on:
                # 用独立线程做 IO + Argon2id 计算，不阻塞事件循环
                await asyncio.to_thread(self._bootstrap_admin_password)
                await self._start_webui()
        except Exception:
            # 初始化中途失败：回滚已创建的资源，避免端口/连接/浏览器残留
            logger.exception("[奴隶市场] 初始化失败，正在回收已创建的资源")
            await self.terminate()
            raise
        logger.info(f"[奴隶市场] 插件已加载，共注册 {_ROUTE_COUNT} 条指令路由")

    def _bootstrap_admin_password(self) -> None:
        """未设过 WebUI 密码时初始化密码哈希。

        优先级：配置里显式设了 webui_password → 直接以其建哈希（must_reset=False）；
        否则生成随机临时密码并标记 must_reset=True。

        生成位置：
          - 临时明文（仅临时密码路径）：data/plugin_data/<name>/admin_passwd.txt（仅出现一次）
          - 摘要：data/plugin_data/<name>/admin_passwd.json 包含 Argon2id hash
        """
        store = PasswordStore(self._data_dir() / "admin_passwd.json")
        existing = store.get()
        if existing and existing.get("hash"):
            # 已有哈希：什么都不做（明文密码由 server.py 迁移逻辑透明哈希）
            return
        # 配置里显式设过 webui_password：直接以它建哈希（无需改密），
        # 而不是生成随机临时密码——否则用户配置的密码会被静默忽略。
        configured = str((self.config or {}).get("webui_password", "") or "")
        hasher = Argon2Hasher(
            time_cost=int((self.config or {}).get("webui_hash_time_cost", 3) or 3),
        )
        if configured:
            try:
                rotate_password(
                    store,
                    hasher,
                    configured,
                    must_reset=False,
                )
            except AuthError as e:
                logger.error(f"[奴隶市场] 写入配置密码失败：{e}")
                return
            return
        # 未设配置密码：生成临时明文密码，18 字符，URL-safe base64
        temp = secrets.token_urlsafe(12)
        try:
            rotate_password(
                store,
                hasher,
                temp,
                must_reset=True,
            )
        except AuthUnavailable:
            # argon2-cffi 未安装时，WebUIServer.__init__ 同样会 raise
            # AuthUnavailable 并导致插件加载失败。此分支不可达——保留
            # 仅是为了在异常链中提供更清晰的错误信息。
            logger.error(
                "[奴隶市场] argon2-cffi 未安装，WebUI 无法启动；"
                "请执行 pip install argon2-cffi 后重载插件"
            )
            raise
        except AuthError as e:
            logger.error(f"[奴隶市场] 生成临时密码失败：{e}")
            return
        # Argon2id 模式下：明文只写磁盘这一次，且明确告知用户
        tp = self._data_dir() / TEMP_PASSWORD_FILE
        tp.write_text(
            "首次启动临时密码（仅出现一次，请尽快重置）\n" + temp + "\n",
            "utf-8",
        )
        logger.warning(
            "\n[奴隶市场] ============== 首次启动 ==============\n"
            f"  WebUI 临时管理员密码: {temp}\n"
            f"  (亦写入 {tp})\n"
            "  首次登录会被强制要求改密（Argon2id）；\n"
            "  请立即登录并将密码改为你自己的。\n"
            "[奴隶市场] ========================================"
        )

    async def _start_webui(self):
        host = str(self.config.get("webui_host", "127.0.0.1"))
        port = int(self.config.get("webui_port", 17818))
        password = str(self.config.get("webui_password", ""))
        # 无密码 + 非本机监听 = 管理后台裸奔，直接拒绝启动并给出可操作提示
        if not password and not _is_loopback(host):
            logger.warning(
                "[奴隶市场] WebUI 未设置密码且监听非本机地址，已拒绝启动。"
                "请在插件配置中设置 webui_password，或将 webui_host 改为 127.0.0.1。"
            )
            return
        # WebUIServer.__init__ 会同步做 SessionStore 建表（含 PRAGMA WAL）、
        # 读密码哈希文件等磁盘 I/O，若在事件循环上构造会阻塞整个机器人。
        # 用 to_thread 把构造挪到线程池，只把纯异步的 start() 留在事件循环上。
        self._webui = await asyncio.to_thread(
            WebUIServer, self.ctx, host, port, VERSION, logger, password=password
        )
        try:
            await self._webui.start()
            logger.info(
                f"[奴隶市场] WebUI(aiohttp) 已启动：http://{host}:{port}"
                + (" 🔒" if password else "")
            )
        except PermissionError:
            logger.warning(
                "[奴隶市场] WebUI 启动失败：端口 "
                f"{port} 被系统保留或被防火墙拦截（WinError 10013）。"
                "常见于 Hyper-V/WSL 动态保留端口，请在插件配置中更换 webui_port 后重载。"
            )
            self._webui = None
        except OSError as e:
            logger.warning(
                f"[奴隶市场] WebUI 启动失败（端口 {port} 被占用？）：{e}；"
                "本次运行将没有 WebUI，其余功能不受影响"
            )
            self._webui = None

    async def terminate(self):
        """卸载/热重载：每一步都必须走到，且都不能无限期挂住。

        渲染器关闭是跨进程 IPC，Chromium 卡死时会永久 pending，
        因此每一步都带超时并 finally 兜底，绝不挂死。

        清理顺序：
        1. 拒新指令（清空锁表：持有的锁协程仍在跑，但没人新来，等它们退出）
        2. WebUI 停止接新连接
        3. 渲染器关
        4. DB 关
        """
        # 1. 锁表先清：避免新指令撞上旧 loop 上的锁；持有方按协程退出自然释放
        locks = getattr(self, "_player_locks", None)
        if locks is not None:
            try:
                locks.clear()
            except Exception:  # noqa: BLE001
                logger.warning("[奴隶市场] 清理指令锁表异常（已忽略）")

        try:
            if self._webui:
                try:
                    await asyncio.wait_for(self._webui.stop(), timeout=15)
                    logger.info("[奴隶市场] WebUI 已停止，端口已释放")
                except Exception as e:  # noqa: BLE001 - 停止失败不阻断卸载
                    logger.warning(f"[奴隶市场] WebUI 停止异常（已忽略）：{e}")
                finally:
                    self._webui = None
        finally:
            try:
                await asyncio.wait_for(self.ctx.renderer.close(), timeout=15)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[奴隶市场] 渲染器关闭异常（已忽略）：{e}")
            finally:
                try:
                    await asyncio.wait_for(self.ctx.db.close(), timeout=15)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[奴隶市场] 数据库关闭异常（已忽略）：{e}")

    # ------------------------------------------------------------------
    # 数据目录与文案
    # ------------------------------------------------------------------

    def _data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:  # noqa: BLE001 - 兜底路径，保证任何内核版本都能初始化
            return Path("data") / "plugin_data" / PLUGIN_NAME

    @staticmethod
    def _load_copywriting() -> dict:
        import json

        base = Path(__file__).parent / "resources" / "data"
        # 打工文案 + 决斗/排位赛（动作/对手/事件/段位）文案，均随插件发布固定。
        # 运行时经 WebUI 保存后由 ctx.set_copywriting 热更新，改文案无需改代码。
        files = {
            "workCopywriting.json": {
                "slaveowner": ["靠着家族的资助，获得收入"],
                "success": ["搬了一天的砖，获得收入"],
                "failure": ["摸鱼被抓了个正着，一分没挣着,[A]身价下降[C]->[D]"],
                "expenses": ["你为奴隶购买了新饰品，花费了15金币。"],
                "buyMaster": ["对不起，人家是尊贵的大奴隶主，不可以购买捏~"],
            },
            "gameTexts.json": {
                "arena_actions": ["使出浑身解数"],
                "ranking_opponents": [
                    {"name": "流浪剑客", "score": 800, "specialEffect": "剑术精湛，容易造成暴击"}
                ],
                "ranking_events": [
                    {"name": "天气晴朗", "effect": 1.1, "desc": "状态绝佳"}
                ],
                "ranking_tiers": [[1000, "青铜"]],
            },
        }
        merged: dict = {}
        for fname, fallback in files.items():
            try:
                data = json.loads((base / fname).read_text("utf-8"))
            except (OSError, ValueError) as e:
                logger.warning(f"[奴隶市场] 文案文件 {fname} 加载失败，使用内置文案：{e}")
                data = {}
            for k, fv in fallback.items():
                v = data.get(k)
                if isinstance(v, list) and v:
                    merged[k] = v
                else:
                    merged[k] = fv
        return merged


# 安装全部指令路由（handlers/ 目录按业务域维护）
# 用真实安装数量打日志：AstrBot 升级导致绑定失效时，日志不应仍然显示"已注册 N 条"
_ROUTE_COUNT = install(SlaveMarket, filter, __name__, ALL_ROUTES)
