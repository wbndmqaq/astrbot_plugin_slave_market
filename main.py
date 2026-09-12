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

from .core.auth import (
    Argon2Hasher,
    AuthError,
    AuthUnavailable,
    PasswordStore,
    rotate_password,
)
from .core.context import VERSION, GameCtx
from .core.texts import load_copywriting
from .handlers import ALL_ROUTES, install
from .webui.server import (
    TEMP_PASSWORD_FILE,
    WebUIServer,
    _is_loopback_host,
    _safe_int,
)

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
                # WebUI 属可选功能：缺 argon2-cffi / PyJWT 时只跳过面板，
                # 绝不让整个插件（全部游戏指令）加载失败。README 也一直把
                # 这些依赖描述为可选。
                try:
                    # 用独立线程做 IO + Argon2id 计算，不阻塞事件循环
                    await asyncio.to_thread(self._bootstrap_admin_password)
                    await self._start_webui()
                except AuthUnavailable as e:
                    self._webui = None
                    logger.error(
                        "[奴隶市场] WebUI 依赖缺失（%s），本次运行将不启动管理面板；"
                        "游戏指令不受影响。需要面板请执行："
                        "pip install argon2-cffi pyjwt，然后重载插件。",
                        e,
                    )
                except Exception as e:  # noqa: BLE001 - WebUI 只降级，绝不致命
                    # WebUI 专属故障（配置脏值、端口/防火墙、磁盘写入、会话库…）
                    # 一律只跳过面板：面板是可选功能，一处问题不该让**全部游戏
                    # 指令**一起失效。这里刻意不上抛。
                    self._webui = None
                    logger.error(
                        "[奴隶市场] WebUI 初始化失败（%s），本次运行将不启动管理面板；"
                        "游戏指令不受影响。请检查 webui_port / webui_hash_time_cost "
                        "等 webui_* 配置项后重载插件。",
                        e,
                    )
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

        密码元数据已存在（admin_passwd.json 里有 hash）时，**不能静默忽略配置里的
        webui_password**：常见流程是"先在环回跑过一次拿到临时密码 → 之后到 AstrBot
        配置里填 webui_password"，旧实现直接 return，用户配置的密码永不生效，
        面板还会在首次登录后强制改密，等于把自己锁在门外。现在的语义：

          - 配置为空 → 什么都不做（保持既有哈希）。
          - 配置明文与既有哈希匹配 → 什么都不做。
          - 不匹配 → 用配置明文轮换哈希（must_reset=False）并记一条 info。

        生成位置：
          - 临时明文（仅临时密码路径）：data/plugin_data/<name>/admin_passwd.txt（仅出现一次）
          - 摘要：data/plugin_data/<name>/admin_passwd.json 包含 Argon2id hash
        """
        store = PasswordStore(self._data_dir() / "admin_passwd.json")
        # 配置脏值（""/None/"abc"）不能抛：initialize() 的 WebUI 分支只降级不
        # 致命，但 _safe_int 让这里根本不会抛，范围与 schema 的 1~10 一致。
        hasher = Argon2Hasher(
            time_cost=_safe_int(
                (self.config or {}).get("webui_hash_time_cost", 3), 3, 1, 10
            ),
        )
        configured = str((self.config or {}).get("webui_password", "") or "")
        existing = store.get()
        if existing and existing.get("hash"):
            if not configured:
                return
            # verify 是 Argon2id 计算（CPU 密集）：本方法整体跑在 to_thread 里
            if hasher.verify(existing["hash"], configured):
                return
            try:
                rotate_password(store, hasher, configured, must_reset=False)
            except AuthError as e:
                logger.error(f"[奴隶市场] 按配置重置 WebUI 密码失败：{e}")
                return
            logger.info(
                "[奴隶市场] 磁盘上的 WebUI 密码哈希与配置里的 webui_password "
                "不一致，已按配置重置（配置项从此生效）"
            )
            return
        # 配置里显式设过 webui_password：直接以它建哈希（无需改密），
        # 而不是生成随机临时密码——否则用户配置的密码会被静默忽略。
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
            # argon2-cffi / PyJWT 未安装：WebUIServer.__init__ 同样会 raise
            # AuthUnavailable。initialize() 捕获它并跳过 WebUI（不整插件失败），
            # 这里只补一条更可操作的错误说明。
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

    def _disk_password_state(self) -> tuple[bool, bool]:
        """磁盘上的 WebUI 密码状态 -> (是否有哈希, 是否仍是待改的临时密码)。

        同步读盘；调用方经 to_thread 调用。与 `WebUIServer.__init__` 的 auth_on
        判定同口径：admin_passwd.json 里有 hash = 面板本来就有密码保护，
        包含「先用生成的临时密码登录过」的部署。
        """
        st = PasswordStore(self._data_dir() / "admin_passwd.json").get() or {}
        return bool(st.get("hash")), bool(st.get("must_reset"))

    async def _start_webui(self):
        host = str(self.config.get("webui_host", "127.0.0.1"))
        # 端口同样走容错转换：""/None/"abc" → 默认 17818，越界夹到 1~65535
        # （与 _conf_schema.json 的 min/max 一致）。裸 int() 会抛 ValueError，
        # 让 WebUI 配置脏值连累整个插件加载。
        port = _safe_int(self.config.get("webui_port", 17818), 17818, 1, 65535)
        password = str(self.config.get("webui_password", "") or "")
        # 只有「配置里没密码」且「磁盘上也没有哈希」才是真的无密码面板，
        # 这时才拒绝非本机监听。只看 config["webui_password"] 会把
        # 「先用临时密码登录过（admin_passwd.json 里已有哈希）」的部署误判成
        # 无密码，日志还会说错原因（实际是被门禁挡住，而不是没设密码）。
        #
        # 但只判「有没有哈希」又太松：临时密码同样是哈希，而它同时以明文躺在
        # data/plugin_data/<插件名>/admin_passwd.txt 里，运维从没主动选过它。
        # 把这种「运维未显式设定过密码」的状态暴露到非本机监听上，等于用一个
        # 明文落盘的口令守住公网后台 —— 所以 must_reset 未清时继续拒绝，
        # 直到运维在配置里填密码、或登录后在面板改一次密码（两者都会清掉它）。
        # 读盘是磁盘 IO：挪到线程池，不在事件循环上读。
        has_hash, must_reset = await asyncio.to_thread(self._disk_password_state)
        if not _is_loopback(host) and not password and (not has_hash or must_reset):
            reason = (
                "磁盘上还没有 admin_passwd.json 里的密码哈希"
                if not has_hash
                else "当前密码仍是 admin_passwd.json 里自动生成的临时密码（从未改过）"
            )
            logger.warning(
                "[奴隶市场] WebUI 已拒绝启动：监听非本机地址"
                f"（webui_host={host}）而运维尚未显式设置过密码（{reason}）——"
                "这等于把管理后台裸奔在网络上（临时密码还有明文落盘）。"
                "请任选一种修复：①在插件配置中设置 webui_password；"
                "②把 webui_host 改回 127.0.0.1；"
                "③先在环回地址下用临时密码登录并在面板里改掉密码，再改为非本机监听。"
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

    def _load_copywriting(self) -> dict:
        """加载游戏文案（打工 + 决斗/排位赛）。

        读取逻辑与兜底文案的唯一来源在 core/texts.py：内置 resources/data/*.json
        与用户覆盖 data_root/overrides/data/*.json 合并，用户覆盖优先。
        运行时经 WebUI 保存后由 ctx.set_copywriting 热更新，改文案无需改代码。
        同步方法：由调用方经 asyncio.to_thread 调用。
        """
        return load_copywriting(self.ctx.data_root)


# 安装全部指令路由（handlers/ 目录按业务域维护）
# 用真实安装数量打日志：AstrBot 升级导致绑定失效时，日志不应仍然显示"已注册 N 条"
_ROUTE_COUNT = install(SlaveMarket, filter, __name__, ALL_ROUTES)
