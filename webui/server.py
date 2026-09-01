"""独立端口 WebUI（aiohttp + Argon2id 密码哈希 + JWT 会话 + 服务端会话表）。

安全模型：
- 密码：仅以 Argon2id 哈希形式落盘（auth.PasswordStore），明文不出现在磁盘。
- 会话：JWT(HS256) 作为客户端 cookie，服务端维护 SQLite 会话表
  （auth.SessionStore），登出/换密/重启直接吊销。
- WebUI 默认启用：插件首次启动会生成临时随机密码（12 字节熵 / 16 字符
  URL-safe），写到 data_dir/admin_passwd.txt 与日志，**仅出现一次**。
  首次登录后会要求强制改密（must_reset=True），改密后临时文件被删除。
- CSRF / Origin 校验 / 同源比对 / 失败限速与原版一致。
"""

import asyncio
import ipaddress
import json
import math
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import web

from ..core.auth import (
    Argon2Hasher,
    AuthError,
    AuthUnavailable,
    JWTIssuer,
    PasswordStore,
    SessionStore,
    rotate_password,
)

COOKIE = "slvm_session"
TTL_DEFAULT = 12 * 3600
PAGE_SIZE = 20
CSRF_HEADER = "X-Slvm-Req"  # 前端写操作必带；跨站请求无法附加自定义头
# 这些键的值不回传前端；保存时留空 = 保持原值
CONFIG_HIDDEN_KEYS = {"webui_password"}
# 无需登录的端点（其余 /api/ 全部强制鉴权）
PUBLIC_PATHS = {"/api/meta", "/api/auth/login", "/api/auth/check"}
# 登录限速：同一 IP 在窗口内的最大失败次数
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW = 300
_STR_MAX = 512  # 字符串配置单值最大长度
_LIST_MAX = 500  # 列表配置最大条目数
_TEXT_ITEMS_MAX = 2000  # 单个文案键最多条目数
_TEXT_LEN_MAX = 500  # 单条文案最大长度
_BAD = object()  # _cast 的失败哨兵（与 None 区分：None 可能是合法值）
# workCopywriting 必须存在且非空的键：缺一个就会让打工指令直接抛异常
_COPY_REQUIRED = ("slaveowner", "success", "failure", "expenses", "buyMaster")

# 临时密码文件名（启动一次后建议重命名/删除以免泄漏）
TEMP_PASSWORD_FILE = "admin_passwd.txt"


def _json(obj, status=200):
    return web.Response(
        # allow_nan=False：inf/NaN 会被 json.dumps 写成 Infinity，
        # 那是非法 JSON，浏览器 JSON.parse 直接失败导致整页打不开
        text=json.dumps(obj, ensure_ascii=False, allow_nan=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _is_loopback_host(host: str) -> bool:
    h = str(host or "").split("%")[0].strip().strip("[]").lower()
    if ":" in h and h.count(":") == 1 and not h.startswith(":"):  # host:port
        h = h.rsplit(":", 1)[0]
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


@web.middleware
async def _error_middleware(request, handler):
    """全局兜底：任何端点内部异常都返回 JSON 错误，而不是裸 500。

    细节只写日志，不回传前端。
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        srv = request.app.get("slvm_server")
        if srv is not None:
            srv.log.exception("[奴隶市场] WebUI 处理 %s 时出错", request.path)
        return _json({"error": "内部错误，请查看 AstrBot 日志"}, 500)


class WebUIServer:
    def __init__(self, ctx, host, port, version, logger, password=""):
        self.ctx = ctx
        self.host = host or "0.0.0.0"
        self.port = int(port)
        self.version = version
        self.log = logger
        # password 仅用于"启动时是否已经配置过密码"的兼容判断；
        # 真正验证走 Auth 层的 PasswordStore（在磁盘上是 Argon2id 哈希）。
        self.legacy_password = str(password or "")
        self.auth_on = bool(self.legacy_password)
        # JWT secret 用安全随机源生成；不暴露给外部模块，只在 server 内使用。
        # 每次进程启动重新生成，老 cookie 立刻失效。
        self._jwt_secret = secrets.token_bytes(48)
        try:
            self._issuer = JWTIssuer(self._jwt_secret)
        except AuthUnavailable as e:
            self.log.error("[奴隶市场] %s，请先 `pip install pyjwt argon2-cffi`", e)
            raise
        # Argon2 哈希器时间成本走 config（默认 3，OWASP 2024 基线）
        time_cost = int(
            (self.ctx.config.get("webui_hash_time_cost") if self.ctx.config else 3) or 3
        )
        try:
            self._hasher = Argon2Hasher(time_cost=time_cost)
        except AuthUnavailable as e:
            self.log.error("[奴隶市场] %s，请先 `pip install pyjwt argon2-cffi`", e)
            raise
        # 服务端会话表与密码哈希文件存到 plugin_data 目录
        data_dir = Path(self.ctx.data_root)
        self._sessions = SessionStore(data_dir / "webui_sessions.db")
        # 启动时清理过期会话，避免 webui_sessions.db 无限膨胀
        try:
            self._sessions.gc()
        except Exception:  # noqa: BLE001
            self.log.warning("[奴隶市场] 启动时会话清理失败（已忽略）")
        self._pwd_store = PasswordStore(data_dir / "admin_passwd.json")
        # 启动时若没有密码记录，把调用方传过来的 password 当作"已存在的密码"
        # 写一次哈希进去（仅当 legacy_password 不为空），兼容配置文件里的明文
        # webui_password，启动时迁移到哈希存储。
        existing = self._pwd_store.get()
        # 旧版明文密码迁移：__init__ 不能 await，把需要做的 Argon2id 哈希
        # 推迟到 start() 里经 asyncio.to_thread 执行，不阻塞事件循环
        self._pending_legacy_migration = False
        if not existing or not existing.get("hash"):
            if self.legacy_password:
                self._pending_legacy_migration = True
            else:
                # 没有 legacy password 也不存在密码文件：留给 main.initialize()
                # 在启动前/后生成临时密码；这里不主动设置 must_reset。
                pass
        else:
            self.auth_on = True
        # 其余字段
        self.dir = Path(__file__).parent
        self._runner = None
        self._cfg_lock = asyncio.Lock()  # 串行化配置保存，避免交错写坏配置文件
        self._texts_lock = asyncio.Lock()  # 串行化文案写盘
        self._fails: dict[str, list[float]] = {}  # IP -> 失败时间戳（登录限速）

    # ---------- 认证工具 ----------

    def _ttl(self) -> int:
        try:
            return max(
                300,
                min(
                    604800,
                    int(self.ctx.config.get("webui_session_ttl") or TTL_DEFAULT),
                ),
            )
        except Exception:  # noqa: BLE001
            return TTL_DEFAULT

    def _session_ttl_seconds(self) -> int:
        return self._ttl()

    async def _authed_token(self, request) -> dict | None:
        """从 cookie 取 JWT，校验签名与有效期，再核验服务端会话表。

        返回 JWT payload 或 None（任何一步失败都给 None 而不抛异常，避免泄漏
        "为什么失败"的信息；上层中间件收到 None 走 401 即可）。

        会话表查询是同步 sqlite3，必须走 to_thread：主库忙时裸调会把
        事件循环卡在 busy_timeout 上（最长 5s），拖停全部群消息。
        """
        raw = request.cookies.get(COOKIE, "")
        if not raw:
            return None
        try:
            payload = self._issuer.verify(raw)
        except AuthError:
            return None
        # 服务端二次校验：JWT 过了不代表服务端还认这个 jti
        if not await asyncio.to_thread(self._sessions.exists, payload.get("jti", "")):
            return None
        return payload

    async def _authed_subject(self, request) -> str | None:
        p = await self._authed_token(request)
        return p.get("sub") if p else None

    def _must_reset(self) -> bool:
        st = self._pwd_store.get()
        return bool(st and st.get("must_reset"))

    def _unauth(self):
        return _json({"error": "未登录"}, 401)

    def _set_session_cookie(
        self,
        resp: web.Response,
        token: str,
        *,
        ttl: int | None = None,
    ) -> None:
        max_age = ttl if ttl is not None else self._ttl()
        resp.set_cookie(
            COOKIE,
            token,
            max_age=max_age,
            httponly=True,
            samesite="Lax",
            path="/",
            secure=False,  # 反向代理 TLS 时改 True 需要配置
        )

    # ---------- Host 与鉴权中间件 ----------

    def _csrf_ok(self, request) -> bool:
        """写操作的跨站防护。

        有密码时靠 SameSite=Lax cookie（跨站 POST 不带 cookie）即可；
        无密码时鉴权完全放行，必须靠"Host 是环回 + 自定义头"这两道，
        否则本机浏览器打开的任意网页都能驱动全部管理 API。
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        origin = request.headers.get("Origin")
        if origin:
            # 严格同源比对：scheme+host+port 必须完全相等。
            # urlparse 会拆掉 path/query，校验只比对真正的 netloc。
            from urllib.parse import urlparse

            try:
                o = urlparse(origin)
            except ValueError:
                return False
            if o.scheme not in ("http", "https"):
                return False
            req_host = request.headers.get("Host", "")
            if (o.hostname or "").lower() != req_host.split(":", 1)[0].lower():
                return False
            try:
                o_port = o.port
            except ValueError:
                return False
            o_port = 80 if o_port is None else o_port
            if ":" in req_host:
                h_port = int(req_host.rsplit(":", 1)[1])
            else:
                h_port = 80 if o.scheme == "http" else 443
            if o_port != h_port:
                return False
        return bool(request.headers.get(CSRF_HEADER))

    def _host_ok(self, request) -> bool:
        """无密码模式下要求 Host 是环回地址。

        这条对**读接口也必须生效**：否则 DNS rebinding 把攻击者域名解析到
        127.0.0.1 后，请求在浏览器看来是同源，CORS 不再拦响应，
        `/api/players_all`、`/api/admin/config` 等会被整体读走。
        """
        if self.auth_on:
            return True  # 有密码时由 cookie 鉴权兜住
        return _is_loopback_host(request.headers.get("Host", ""))

    def _login_blocked(self, ip: str, now: float) -> bool:
        """返回 True 表示该 IP 当前在限速窗口内、应返回 429。

        维护策略：
        - 每次调用都按 LOGIN_WINDOW 过滤一次该 IP 的失败时间戳，过期剔除，
          避免失败记录长期累积占内存。
        - 已限速 IP（hits >= LOGIN_MAX_FAILS）不能被容量保护策略淘汰掉，
          否则攻击者把 IP 撞到限速后，可让另一个 IP 失败到容量上限来清除
          原限速记录绕过限制。
        - 总容量 1000 上限只约束"未限速 IP"；限速 IP 跟随一次性涨到 ~2**32。
          实际 1000 个限速 IP 已足够覆盖常见的攻击规模。
        """
        existing = self._fails.get(ip, [])
        hits = [t for t in existing if now - t < LOGIN_WINDOW]
        if hits:
            self._fails[ip] = hits
        else:
            self._fails.pop(ip, None)
            return False
        # 容量兜底：清理那些"未达限速上限 + 也没在最近窗口里刷过存在感"的 IP。
        # 已限速 IP 命中"hits >= LOGIN_MAX_FAILS"，永不被视作 stale。
        if len(self._fails) > 1000:
            stale = [
                k
                for k in list(self._fails)
                if len(self._fails.get(k, [])) < LOGIN_MAX_FAILS
            ]
            for k in stale:
                self._fails.pop(k, None)
                if len(self._fails) <= 1000:
                    break
        return len(hits) >= LOGIN_MAX_FAILS

    @web.middleware
    async def _guard(self, request, handler):
        """统一鉴权 + 防跨站：新增端点无需再写一遍检查。

        顺序：
        1. Host 校验（防 DNS rebinding）
        2. CSRF 校验（method = unsafe）
        3. 公开白名单放行
        4. JWT + 服务端会话校验；并在 must_reset 期间只允许 change-password
        """
        path = request.path
        if path.startswith("/api/"):
            # Host 校验对读写都生效（无密码模式下防 DNS rebinding 脱库）
            if not self._host_ok(request):
                return _json({"error": "请求被拒绝（仅允许本机访问）"}, 403)
            if not self._csrf_ok(request):
                return _json({"error": "请求被拒绝（跨站保护）"}, 403)
            if path not in PUBLIC_PATHS:
                # 无密码模式（仅本机监听 + 写操作带自定义头）下鉴权完全放行：
                # 此时不会签发 JWT，若仍强行要求 token，前端所有需要鉴权的
                # 接口（/api/overview、/api/groups 等）都会拿到 401，面板整体不可用。
                if self.auth_on:
                    if not await self._authed_token(request):
                        return self._unauth()
                    # 强制改密期间：除 change-password / logout / meta / check 外，
                    # 全部以 423 拒绝，前端用它触发 reset overlay
                    if self._must_reset() and path not in (
                        "/api/auth/change-password",
                        "/api/auth/logout",
                        "/api/auth/check",
                        "/api/meta",
                    ):
                        return _json(
                            {"error": "首次登录必须重置密码", "must_reset": True},
                            423,
                        )
        return await handler(request)

    async def _body(self, request) -> tuple[dict, web.Response | None]:
        """安全解析 JSON 请求体 -> (dict, 错误响应)。非法输入返回 400 而不是 500。"""
        if not request.can_read_body:
            return {}, None
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}, _json({"error": "请求体不是合法 JSON"}, 400)
        if not isinstance(data, dict):
            return {}, _json({"error": "请求体必须是 JSON 对象"}, 400)
        return data, None

    @staticmethod
    def _index_arg(body: dict) -> int | None:
        try:
            n = int(body.get("index", 0))
        except (TypeError, ValueError):
            return None
        return n if n >= 1 else None

    async def start(self):
        # 旧版明文密码迁移（Argon2id 哈希是 CPU 密集型，不能在 __init__ 里同步做）
        if getattr(self, "_pending_legacy_migration", False):
            try:
                await asyncio.to_thread(
                    rotate_password,
                    self._pwd_store,
                    self._hasher,
                    self.legacy_password,
                    must_reset=False,
                )
                self.auth_on = True
                self.log.info(
                    "[奴隶市场] WebUI 旧版明文密码已迁移为 Argon2id 哈希（首次登录后建议改密）"
                )
            except Exception as e:  # noqa: BLE001
                self.log.error("[奴隶市场] 旧版密码迁移失败：%s", e)
            self._pending_legacy_migration = False
        app = web.Application(middlewares=[_error_middleware, self._guard])
        app["slvm_server"] = self
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/webui/style.css", self._style_css)
        r.add_get("/webui/app.js", self._app_js)
        r.add_get("/api/meta", self._meta)
        r.add_post("/api/auth/login", self._login)
        r.add_post("/api/auth/logout", self._logout)
        r.add_post("/api/auth/change-password", self._change_password)
        r.add_get("/api/auth/check", self._check)
        r.add_get("/api/overview", self._overview)
        r.add_get("/api/groups", self._groups)
        r.add_get("/api/ranking", self._ranking)
        r.add_get("/api/market", self._market)
        r.add_get("/api/players", self._players)
        r.add_get("/api/players_all", self._players_all)
        r.add_get("/api/search", self._search)
        r.add_get("/api/admin/player", self._admin_get)
        r.add_post("/api/admin/player/save", self._admin_save)
        r.add_post("/api/admin/player/delete", self._admin_delete)
        r.add_get("/api/backups", self._backup_list)
        r.add_post("/api/backups/create", self._backup_create)
        r.add_post("/api/backups/restore", self._backup_restore)
        r.add_post("/api/backups/delete", self._backup_delete)
        r.add_get("/api/admin/config", self._admin_config)
        r.add_post("/api/admin/config/save", self._admin_config_save)
        r.add_get("/api/admin/texts", self._texts_get)
        r.add_post("/api/admin/texts/save", self._texts_save)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
        await self._runner.setup()
        # 端口占用(EADDRINUSE)做短重试——旧实例完全释放前有一小段失败窗口；
        # PermissionError(10013)=端口被系统保留或防火墙拦截，重试无意义。
        # 只构造一个 site 反复 start，避免把失败的 site 一个个挂在 runner 上。
        site = web.TCPSite(self._runner, self.host, self.port, reuse_address=True)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await site.start()
                return
            except PermissionError as e:
                last_exc = e
                break
            except OSError as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(0.6)
        await self._runner.cleanup()
        self._runner = None
        raise last_exc  # type: ignore[misc]

    async def stop(self):
        if self._runner:
            runner, self._runner = self._runner, None
            try:
                # cleanup 内部会先 await site.stop() 拒新连接、再关闭，
                # shutdown_timeout 控制关闭 site 后给 in-flight handler 的
                # 收尾窗口。10s 覆盖大多数请求（截图/DB 都不该超过这个数）
                # 又不会让卸载永远卡在异常 handler 上。
                await asyncio.wait_for(runner.cleanup(), timeout=15)
            except asyncio.TimeoutError:
                # 仍有 handler 占用：强制返回，端口由系统回收
                # （in-flight handler 在 cleanup() 内部会被 cancel，但
                # db.transact 的 to_thread worker 不在 loop 上，cancel 不到，
                # 这里超时就别再等，让上层继续走 db.close 的 15s 兜底）
                pass

    # ===== 静态文件 =====

    async def _index(self, request):
        return await self._file("index.html", "text/html")

    async def _style_css(self, request):
        return await self._file("style.css", "text/css")

    async def _app_js(self, request):
        return await self._file("app.js", "application/javascript")

    async def _file(self, fname, ctype):
        try:
            body = await asyncio.to_thread((self.dir / fname).read_bytes)
            return web.Response(
                body=body,
                content_type=ctype,
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Frame-Options": "DENY",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "frame-ancestors 'none'",
                },
            )
        except OSError:
            self.log.exception("[奴隶市场] WebUI 静态文件读取失败：%s", fname)
            return _json({"error": "静态资源缺失"}, 500)

    # ===== 认证 =====

    async def _login(self, request):
        body, err = await self._body(request)
        if err:
            return err
        pwd = str(body.get("password", ""))
        if not self.auth_on:
            # 理论上在非环回监听下不会到这一步：启动期已拦截。
            # 此处兜底：当成"无密码模式"，直接放行 + 把 cookie 当空了。
            return _json({"ok": True, "msg": "未启用密码"})
        ip = request.remote or "?"
        now = time.time()
        # 限速：仅靠 sleep(0.5) 挡不住并发爆破（协程等待不串行化）
        if self._login_blocked(ip, now):
            return _json({"error": "尝试过于频繁，请稍后再试"}, 429)
        st = self._pwd_store.get()
        if not st or not st.get("hash"):
            # 极端情况：登录前被人手工删了哈希文件
            self.log.error("[奴隶市场] 登录失败：密码哈希文件缺失")
            return _json({"error": "尚未初始化管理员密码"}, 503)
        ok = await asyncio.to_thread(self._hasher.verify, st["hash"], pwd)
        if ok and self._hasher.needs_rehash(st["hash"]):
            # 参数升级：透明重哈希，不影响本次登录
            try:
                await asyncio.to_thread(
                    rotate_password,
                    self._pwd_store,
                    self._hasher,
                    pwd,
                    must_reset=bool(st.get("must_reset")),
                )
                self.log.info("[奴隶市场] Argon2id 参数已升级，密码透明重哈希")
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            self._fails.setdefault(ip, []).append(now)
            self.log.warning("[奴隶市场] WebUI 登录失败（%s）", ip)
            await asyncio.sleep(0.5)
            return _json({"error": "密码错误"}, 401)
        # 校验通过：先清限速记录，再发 JWT + 服务端会话
        self._fails.pop(ip, None)
        ttl = self._ttl()
        token, jti, exp = self._issuer.issue(sub="admin", ttl_seconds=ttl)
        await asyncio.to_thread(
            self._sessions.put,
            jti,
            "admin",
            ip,
            request.headers.get("User-Agent", ""),
            ttl=ttl,
        )
        # 顺带清理过期会话：单条 DELETE，开销极低，避免长期运行时 sessions 表膨胀
        try:
            await asyncio.to_thread(self._sessions.gc)
        except Exception:  # noqa: BLE001
            pass
        resp = _json(
            {
                "ok": True,
                "must_reset": bool(st.get("must_reset")),
                "ttl": ttl,
            }
        )
        self._set_session_cookie(resp, token, ttl=ttl)
        # 成功登录后，清理可能存在的临时密码文件（即便还有，下一次登录也走哈希）
        await self._cleanup_temp_password_file()
        return resp

    async def _logout(self, request):
        payload = await self._authed_token(request) or {}
        jti = payload.get("jti") if payload else None
        if jti:
            await asyncio.to_thread(self._sessions.revoke, jti)
        resp = _json({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def _check(self, request):
        st = self._pwd_store.get()
        must_reset = bool(st and st.get("must_reset"))
        if not self.auth_on:
            return _json(
                {
                    "required": False,
                    "ok": True,
                    "must_reset": False,
                }
            )
        payload = await self._authed_token(request)
        ok = payload is not None
        return _json(
            {
                "required": True,
                "ok": ok,
                "must_reset": must_reset,
                "show_password_plain": bool(
                    (
                        self.ctx.config.get("webui_show_password_plain")
                        if self.ctx.config
                        else False
                    )
                ),
            }
        )

    async def _change_password(self, request):
        """重置管理员密码。要求已登录；JSON {old_password?, new_password}。

        - old_password：常规改密时必填；首次改密（must_reset=True）可省略
          （因为登录时已经用旧密码过过一次，本次就当切换）。
        - new_password：6~128 字节，UTF-8 编码后计算长度。
        - 改密成功 -> rotate_password 写入新哈希 + 清 must_reset；
          同步撤销"旧密码对应的所有服务端会话"以外的会话（即只保留本次
          这个新会话，让其它设备 / 标签页全部下线）。
        """
        body, err = await self._body(request)
        if err:
            return err
        new_pwd = str(body.get("new_password", ""))
        old_pwd = str(body.get("old_password", ""))
        st = self._pwd_store.get() or {}
        must_reset = bool(st.get("must_reset"))
        # 校验 old：must_reset=True 时跳过（前端流程已经登录过一次了）
        if not must_reset:
            if not old_pwd or not await asyncio.to_thread(
                self._hasher.verify, st.get("hash", ""), old_pwd
            ):
                return _json({"error": "旧密码错误"}, 401)
        # 校验 new
        new_bytes = new_pwd.encode("utf-8") if new_pwd else b""
        if len(new_bytes) < 6 or len(new_bytes) > 128:
            return _json({"error": "新密码长度需在 6~128 字节之间（UTF-8）"}, 400)
        try:
            await asyncio.to_thread(
                rotate_password,
                self._pwd_store,
                self._hasher,
                new_pwd,
                must_reset=False,
            )
        except AuthError as e:
            return _json({"error": f"密码哈希失败：{e}"}, 400)
        # 吊销除当前 jti 之外的所有会话
        payload = await self._authed_token(request) or {}
        cur_jti = payload.get("jti")
        # 全部先吊销
        n = await asyncio.to_thread(self._sessions.revoke_all)
        # 再把当前会话放回去（JWT 仍有效，直接重写会话表即可）
        if cur_jti:
            ip = request.remote or ""
            ua = request.headers.get("User-Agent", "")
            await asyncio.to_thread(
                self._sessions.put, cur_jti, "admin", ip, ua, ttl=self._ttl()
            )
        self.log.info("[奴隶市场] 管理员密码已重置，顺带吊销 %d 条会话", n)
        return _json({"ok": True, "revoked": n})

    async def _cleanup_temp_password_file(self) -> None:
        """登录成功后清理临时密码文件（覆盖再删，防磁盘恢复还原）。"""
        path = self.ctx.data_root / TEMP_PASSWORD_FILE

        def _do() -> None:
            try:
                if path.exists():
                    try:
                        path.write_text(
                            "已使用\n" + secrets.token_hex(64),
                            "utf-8",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 - 任何清理失败都不阻断登录
                pass

        await asyncio.to_thread(_do)

    # ===== 公开 =====

    async def _meta(self, request):
        return _json(
            {
                "name": "astrbot_plugin_slave_market",
                "display": "奴隶市场",
                "version": self.version,
                "port": self.port,
                "auth_required": self.auth_on,
                "page_size": PAGE_SIZE,
                "csrf_header": CSRF_HEADER,
                "now": int(time.time()),
            }
        )

    # ===== 鉴权端点（鉴权由 _guard 中间件统一完成） =====

    async def _overview(self, request):
        return _json({"stats": await self.ctx.service.stats()})

    async def _groups(self, request):
        return _json({"groups": await self.ctx.service.group_counts()})

    async def _ranking(self, request):
        gid = request.query.get("gid", "")
        kind = request.query.get("kind", "currency")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        kind = kind if kind in ("currency", "value", "slave", "bank") else "currency"
        r = await self.ctx.service.leaderboard(gid, kind)
        return _json({"kind": kind, "rows": r.get("data", {}).get("rows", [])})

    async def _market(self, request):
        gid = request.query.get("gid", "")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        r = await self.ctx.service.market_list(gid)
        return _json({"items": r.get("data", {}).get("items", [])})

    async def _players(self, request):
        gid = request.query.get("gid", "")
        if not gid:
            return _json({"error": "缺 gid"}, 400)
        try:
            page = max(1, int(request.query.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        try:
            size = min(200, max(1, int(request.query.get("size", str(PAGE_SIZE)))))
        except (TypeError, ValueError):
            size = PAGE_SIZE
        # 分页下推到 SQL，避免全量读取再切片
        total, players = await self.ctx.service.page_profiles(gid, page, size)
        return _json({"total": total, "page": page, "size": size, "players": players})

    async def _search(self, request):
        gid = request.query.get("gid", "")
        kw = request.query.get("kw", "").strip().lower()[:64]
        if not gid or not kw:
            return _json({"results": []})
        profiles = await self.ctx.service.all_profiles(gid)
        results = [
            p for p in profiles if kw in p["uid"].lower() or kw in p["nickname"].lower()
        ][:20]
        return _json({"results": results})

    async def _players_all(self, request):
        """全部玩家（跨群），供免冷却选择器等使用。"""
        out = []
        for g in await self.ctx.service.group_counts():
            for p in await self.ctx.service.all_profiles(g["gid"]):
                out.append(
                    {"gid": g["gid"], "uid": p["uid"], "nickname": p["nickname"]}
                )
        return _json({"players": out})

    # ===== 玩家管理 =====

    async def _admin_get(self, request):
        gid = request.query.get("gid", "")
        uid = request.query.get("uid", "")
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)
        if not await self.ctx.service.db.exists(gid, uid):
            return _json({"error": "未找到"}, 404)
        data = await self.ctx.service.db.load(gid, uid)
        return _json({"profile": self.ctx.service.profile_of(gid, uid, data)})

    @staticmethod
    def _finite(raw) -> float | None:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    async def _admin_save(self, request):
        body, err = await self._body(request)
        if err:
            return err
        gid = str(body.get("gid", ""))
        uid = str(body.get("uid", ""))
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)
        if not await self.ctx.service.db.exists(gid, uid):
            return _json({"error": "未找到"}, 404)

        db = self.ctx.service.db
        rejected: list[str] = []

        def _apply(tx):
            data = tx.get(uid)
            for key, (path, caster, lo) in {
                "currency": (("currency",), float, 0.0),
                "value": (("value",), float, 0.0),
                "bank_level": (("bank", "level"), int, 1),
                "bank_balance": (("bank", "balance"), float, 0.0),
            }.items():
                if key not in body:
                    continue
                v = self._finite(body[key])
                if v is None:
                    rejected.append(key)
                    continue
                v = caster(max(lo, v))
                if len(path) == 1:
                    data[path[0]] = v
                else:
                    data[path[0]][path[1]] = v
            # 主人变更必须双向维护：只改自己的 master 会留下"我认他为主、
            # 他名下却没有我"的单向关系，赎身/出售等逻辑会算错
            if "master" in body:
                new_master = str(body.get("master") or "").strip()
                old_master = str(data.get("master") or "")
                if new_master != old_master:
                    # 先校验新值再动数据：否则填错一个 ID 就把原本合法的
                    # 主奴关系解绑掉，奴隶白白变成自由人
                    if new_master and new_master == uid:
                        rejected.append("master（不能是自己）")
                    elif new_master and not tx.exists(new_master):
                        rejected.append("master（该玩家不存在）")
                    else:
                        if old_master:
                            self.ctx.service._drop_slave(tx.get(old_master), uid)
                        if new_master:
                            self.ctx.service._add_slave(tx.get(new_master), uid)
                        data["master"] = new_master
            return data

        data = await db.transact(gid, _apply)
        return _json(
            {
                "ok": True,
                "rejected": rejected,
                "profile": self.ctx.service.profile_of(gid, uid, data),
            }
        )

    async def _admin_delete(self, request):
        body, err = await self._body(request)
        if err:
            return err
        gid = str(body.get("gid", ""))
        uid = str(body.get("uid", ""))
        if not gid or not uid:
            return _json({"error": "缺参数"}, 400)
        if not await self.ctx.service.db.exists(gid, uid):
            return _json({"error": "未找到"}, 404)
        # db.delete 会同时清理其他玩家对该 uid 的 master/slave 引用
        await self.ctx.service.db.delete(gid, uid)
        return _json({"ok": True})

    # ===== 备份 =====

    async def _backup_list(self, request):
        backups = await self.ctx.service.db.list_backups()
        return _json(
            {"backups": [{"index": i + 1, "name": b} for i, b in enumerate(backups)]}
        )

    async def _backup_create(self, request):
        return _json({"ok": True, "name": await self.ctx.service.db.create_backup()})

    async def _backup_restore(self, request):
        body, err = await self._body(request)
        if err:
            return err
        index = self._index_arg(body)
        if index is None:
            return _json({"error": "序号非法"}, 400)
        try:
            name = await self.ctx.service.db.restore_backup(index)
        except ValueError as e:  # 备份损坏，db 层已拒绝覆盖主库
            return _json({"error": str(e)}, 400)
        if not name:
            return _json({"error": "未找到"}, 404)
        return _json({"ok": True, "restored": name})

    async def _backup_delete(self, request):
        body, err = await self._body(request)
        if err:
            return err
        index = self._index_arg(body)
        if index is None:
            return _json({"error": "序号非法"}, 400)
        name = await self.ctx.service.db.delete_backup(index)
        if not name:
            return _json({"error": "未找到"}, 404)
        return _json({"ok": True, "deleted": name})

    # ===== 插件配置 =====

    @staticmethod
    def _read_schema() -> dict:
        sp = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        try:
            return json.loads(sp.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    async def _load_schema(self) -> dict:
        return await asyncio.to_thread(self._read_schema)

    async def _admin_config(self, request):
        schema = await self._load_schema()
        cfg = {}
        show_plain = bool(self.ctx.config.get("webui_show_password_plain"))
        for k, meta in schema.items():
            v = self.ctx.config.get(k, meta.get("default"))
            # 隐藏类配置仅在前端明确请求"明文暂存"时才回传明文，否则恒为空
            if k == "webui_password":
                cfg[k] = v if show_plain else ""
            elif k in CONFIG_HIDDEN_KEYS:
                cfg[k] = ""
            else:
                cfg[k] = v
        return _json(
            {
                "schema": schema,
                "config": cfg,
                "hidden_keys": sorted(CONFIG_HIDDEN_KEYS & set(schema)),
                "show_password_plain": show_plain,
            }
        )

    @staticmethod
    def _clamp(v, meta):
        """按 schema 的 min/max 夹值。没写 min/max 就原样返回。"""
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None:
            v = max(type(v)(lo), v)
        if hi is not None:
            v = min(type(v)(hi), v)
        return v

    def _cast(self, raw, meta, notes: list[str], label: str):
        """按 schema 类型转换单个值。失败返回 _BAD 并记入 notes。"""
        tp = meta.get("type", "string")
        try:
            if tp == "bool":
                if isinstance(raw, str):
                    return raw.strip().lower() in ("1", "true", "on", "yes", "是")
                return bool(raw)
            if tp in ("int", "float"):
                v = self._finite(raw)
                if v is None:
                    notes.append(f"{label}：不是有效数字，已忽略")
                    return _BAD
                return self._clamp(int(v) if tp == "int" else v, meta)
            if tp == "list":
                if isinstance(raw, str):
                    # 前端是"每行一个"，这里只按换行切
                    raw = raw.splitlines()
                if not isinstance(raw, (list, tuple)):
                    notes.append(f"{label}：不是列表，已忽略")
                    return _BAD
                items = [str(x).strip()[:_STR_MAX] for x in raw if str(x).strip()]
                if len(items) > _LIST_MAX:
                    notes.append(f"{label}：超过 {_LIST_MAX} 条，已截断")
                return items[:_LIST_MAX]
            if not isinstance(raw, (str, int, float, bool)):
                notes.append(f"{label}：类型不支持，已忽略")
                return _BAD
            return str(raw).strip()[:_STR_MAX]
        except (TypeError, ValueError, OverflowError):
            notes.append(f"{label}：取值非法，已忽略")
            return _BAD

    async def _admin_config_save(self, request):
        body, err = await self._body(request)
        if err:
            return err
        values = body.get("values") or {}
        if not isinstance(values, dict):
            return _json({"error": "values 必须是对象"}, 400)
        schema = await self._load_schema()
        notes: list[str] = []
        applied = 0
        # 仅当本请求真正"提交了非空密码并走 _do_change_password_to 成功"时，
        # 才在循环外把 self.auth_on 置 True。避免"留空 = 跳过"分支误触发。
        password_changed = False
        async with self._cfg_lock:  # 串行化：并发保存不会持久化半更新状态
            target = self.ctx.config
            for k, raw in values.items():
                meta = schema.get(k)
                if not meta:
                    continue
                if meta.get("type") == "object":
                    if not isinstance(raw, dict):
                        notes.append(f"{k}：不是对象，已忽略")
                        continue
                    # 与现有值合并而非整体替换：未提交的子键不该被静默重置
                    cur = target.get(k)
                    merged = dict(cur) if isinstance(cur, dict) else {}
                    for sk, smeta in (meta.get("items") or {}).items():
                        if sk not in raw:
                            if "default" in smeta:
                                merged.setdefault(sk, smeta["default"])
                            continue
                        sv = self._cast(raw[sk], smeta, notes, f"{k}.{sk}")
                        if sv is not _BAD:
                            merged[sk] = sv
                    target[k] = merged
                    applied += 1
                    continue
                v = self._cast(raw, meta, notes, k)
                if v is _BAD:
                    continue
                if k in CONFIG_HIDDEN_KEYS and v == "":
                    notes.append(f"{k}：留空表示保持原值，未修改")
                    continue
                # 不允许在非环回监听时把密码清空：面板会立刻变成无鉴权，
                # 而"无密码 + 非本机监听"的启动期检查此时已经过去了
                if k == "webui_password" and not v and not _is_loopback_host(self.host):
                    notes.append("当前监听非本机地址，拒绝清空密码")
                    continue
                # webui_password 走单独的"重哈希"通道：存的是明文，磁盘上是 Argon2id
                if k == "webui_password" and v:
                    try:
                        await asyncio.to_thread(self._do_change_password_to, str(v))
                        notes.append("管理员密码已更新（已用 Argon2id 重哈希）")
                        applied += 1
                        password_changed = True
                    except AuthError as e:
                        notes.append(f"密码哈希失败：{e}")
                    continue
                target[k] = v
                applied += 1
            save = getattr(target, "save_config", None)
            persisted = False
            if callable(save):
                # save_config 可能是同步或异步函数；
                # 把 coroutine 塞进 to_thread 会一直 pending，所以分两种调用方式
                import inspect

                if inspect.iscoroutinefunction(save):
                    await save()
                else:
                    await asyncio.to_thread(save)
                persisted = True
            else:
                notes.append("当前配置对象不支持持久化，重载插件后会恢复原值")
        # 以下配置项需立即热更新
        # - session TTL：下次登录生效
        # - Argon2id time_cost：下次 verify/重哈希时生效
        # - show_password_plain：下次 GET /api/admin/config 生效
        if password_changed:
            # 仅在本次请求成功哈希了新密码时同步内存视图；
            # 留空 / 错误 / 未提交 都不会误触发
            self.auth_on = True
        # 渲染倍率即时同步（已启动的浏览器会话在下次重启渲染器后生效）
        if "render_scale" in values:
            scale = self._finite(target.get("render_scale"))
            if scale:
                self.ctx.renderer.scale = max(1.0, min(4.0, scale))
        # 备份份数是构造 PlayerDB 时传进去的，但 _prune 只在 backup_create 时
        # 才被触发，这里改的是下次创建备份时使用的份数；新玩家初始银行参数
        # 则是同步写入 db.set_bank_init，下个新号立刻生效
        if "backupKeep" in values:
            self.ctx.service.db.backup_keep = max(0, int(target.get("backupKeep") or 0))
        if isinstance(values.get("bank"), dict) and any(
            k in values["bank"]
            for k in ("initialLevel", "initialLimit", "initialUpgradePrice")
        ):
            self.ctx.service.db.set_bank_init(target.get("bank") or {})
        # 这些项只在启动时读取，改完必须重载插件才生效
        restart_only = [
            k for k in ("webui_enabled", "webui_host", "webui_port") if k in values
        ]
        if restart_only:
            notes.append(f"{'、'.join(restart_only)} 需重载插件后生效")
        return _json(
            {"ok": True, "applied": applied, "persisted": persisted, "notes": notes}
        )

    def _do_change_password_to(self, plaintext: str) -> None:
        """把磁盘上的密码哈希替换为 plaintext 的 Argon2id 哈希。

        调用方通过 `_admin_config_save` 循环内显式 `password_changed = True`
        来标记，再在循环外统一把 self.auth_on 置位。
        """
        rotate_password(
            self._pwd_store,
            self._hasher,
            plaintext,
            must_reset=False,
        )

    # ===== 文案编辑（游戏文案 + 帮助长文本）=====

    # 文件名 -> 相对 resources/ 的子目录
    _TEXT_DIRS = {"workCopywriting": "data", "gameTexts": "data", "help": "texts"}

    def _texts_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "resources"

    async def _texts_get(self, request):
        name = request.query.get("name", "")
        if name not in self._TEXT_DIRS:
            return _json({"error": "bad name"}, 400)
        p = self._texts_root() / self._TEXT_DIRS[name] / f"{name}.json"
        try:
            raw_text = await asyncio.to_thread(p.read_text, "utf-8")
            data = json.loads(raw_text)
        except (OSError, ValueError):
            return _json({"error": "未找到"}, 404)
        return _json({"name": name, "data": data})

    def _validate_texts(self, name: str, data) -> str | None:
        """返回错误消息；None 表示校验通过。"""
        if not isinstance(data, dict) or not data:
            return "内容为空"
        if name == "gameTexts":
            return self._validate_game_texts(data)
        for k, v in data.items():
            if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]+", k):
                return f"非法键名：{str(k)[:30]}"
            if name == "help":
                if k in ("title", "sub", "text") and not isinstance(v, str):
                    return f"键 {k} 必须是字符串"
                if k == "sections" and not isinstance(v, list):
                    return "键 sections 必须是数组"
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return f"键 {k} 的值必须是字符串数组"
            if len(v) > _TEXT_ITEMS_MAX:
                return f"键 {k} 的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
            for x in v:
                if not x.strip():
                    return f"键 {k} 含空条目，请删除或填写内容"
                if len(x) > _TEXT_LEN_MAX:
                    return f"键 {k} 有条目超过 {_TEXT_LEN_MAX} 字"
                # 打工的意外支出金额是从文案里正则取第一串数字的，
                # 超长数字串会让 int() 在 Python 3.11+ 抛异常，指令直接失败
                if max((len(n) for n in re.findall(r"\d+", x)), default=0) > 12:
                    return f"键 {k} 有条目含超长数字，请改短"
        if name == "help":
            missing = [k for k in ("title", "sections") if not data.get(k)]
            if missing:
                return f"缺少必需键：{'、'.join(missing)}"
            for i, sec in enumerate(data["sections"], 1):
                if not isinstance(sec, dict) or not isinstance(sec.get("items"), list):
                    return f"第 {i} 个分栏结构非法"
                if not all(isinstance(x, str) for x in sec["items"]):
                    return f"第 {i} 个分栏的条目必须是字符串"
        else:
            # 打工文案是按 key 直接下标访问的（copy["slaveowner"] 等），
            # 少一个键或某个键为空数组，会让打工指令直接抛 KeyError/IndexError
            missing = [k for k in _COPY_REQUIRED if not data.get(k)]
            if missing:
                return f"以下文案不能为空：{'、'.join(missing)}"
        return None

    def _validate_game_texts(self, data) -> str | None:
        """决斗/排位赛文案校验：结构非法或内容超限则拒绝保存。

        gameTexts 的键是固定的（service.py 直接按 key 下标访问），缺键或
        类型不对会让决斗/排位赛指令在运行时抛异常，所以这里逐键强校验。
        """
        allowed = {
            "arena_actions",
            "ranking_opponents",
            "ranking_events",
            "ranking_tiers",
            "ranking_top_tier",
        }
        for k, v in data.items():
            if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]+", k):
                return f"非法键名：{str(k)[:30]}"
            if k not in allowed:
                return f"未知键：{k}"
        if "ranking_top_tier" in data:
            v = data["ranking_top_tier"]
            if not isinstance(v, str) or not v.strip() or len(v) > _TEXT_LEN_MAX:
                return f"键 ranking_top_tier 必须是非空字符串（不超过 {_TEXT_LEN_MAX} 字）"
        if "arena_actions" in data:
            v = data["arena_actions"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return "键 arena_actions 的值必须是字符串数组"
            if len(v) > _TEXT_ITEMS_MAX:
                return f"键 arena_actions 的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
            for x in v:
                if not x.strip() or len(x) > _TEXT_LEN_MAX:
                    return f"键 arena_actions 有条目为空或超过 {_TEXT_LEN_MAX} 字"
        if "ranking_opponents" in data:
            v = data["ranking_opponents"]
            if not isinstance(v, list) or not v:
                return "键 ranking_opponents 必须是非空数组"
            for i, o in enumerate(v, 1):
                if not isinstance(o, dict):
                    return f"第 {i} 个对手结构非法"
                if not isinstance(o.get("name"), str) or not o["name"].strip():
                    return f"第 {i} 个对手缺少名称"
                try:
                    int(o.get("score"))
                except (TypeError, ValueError):
                    return f"第 {i} 个对手的 score 必须是数字"
                if not isinstance(o.get("specialEffect"), str):
                    return f"第 {i} 个对手的 specialEffect 必须是字符串"
        if "ranking_events" in data:
            v = data["ranking_events"]
            if not isinstance(v, list) or not v:
                return "键 ranking_events 必须是非空数组"
            for i, e in enumerate(v, 1):
                if not isinstance(e, dict):
                    return f"第 {i} 个事件结构非法"
                if not isinstance(e.get("name"), str) or not e["name"].strip():
                    return f"第 {i} 个事件缺少名称"
                try:
                    float(e.get("effect"))
                except (TypeError, ValueError):
                    return f"第 {i} 个事件的 effect 必须是数字"
                if not isinstance(e.get("desc"), str):
                    return f"第 {i} 个事件的 desc 必须是字符串"
        if "ranking_tiers" in data:
            v = data["ranking_tiers"]
            if not isinstance(v, list) or not v:
                return "键 ranking_tiers 必须是非空数组"
            prev = None
            for i, t in enumerate(v, 1):
                if not isinstance(t, (list, tuple)) or len(t) != 2:
                    return f"第 {i} 个段位必须是 [分数, 名称] 二元组"
                try:
                    score = int(t[0])
                except (TypeError, ValueError):
                    return f"第 {i} 个段位的分数必须是数字"
                if not isinstance(t[1], str) or not t[1].strip():
                    return f"第 {i} 个段位缺少名称"
                if prev is not None and score <= prev:
                    return "段位分数必须严格递增"
                prev = score
        return None

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        """原子写入，并保留一份最初的原始版本。

        - tmp 文件名带 pid+随机后缀：并发保存不会互相截断同一个临时文件
        - .bak 只在不存在时写：连续保存两次也不会让备份变成"上一次的错误版本"
        - Windows 上 os.replace 在源/目标 inode 已被 mmap 时可能留下 0 字节文件，
          所以 replace 后立刻 stat 主文件大小，0 字节立刻从 .bak 还原
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        bak = path.with_suffix(path.suffix + ".bak")
        if path.exists() and not bak.exists():
            try:
                bak.write_text(path.read_text("utf-8"), "utf-8")
            except OSError:
                pass
        tmp = path.with_suffix(
            f"{path.suffix}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            tmp.write_text(content, "utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        # Windows 上 os.replace 偶尔留 0 字节文件（mmap 句柄未释放时）
        try:
            if path.stat().st_size == 0:
                bak_text = bak.read_text("utf-8") if bak.exists() else ""
                path.write_text(bak_text, "utf-8")
        except OSError:
            pass

    async def _texts_save(self, request):
        body, err = await self._body(request)
        if err:
            return err
        name = str(body.get("name") or "")
        data = body.get("data")
        if name not in self._TEXT_DIRS:
            return _json({"error": "bad name"}, 400)
        bad = self._validate_texts(name, data)
        if bad:
            return _json({"error": bad}, 400)
        content = json.dumps(data, ensure_ascii=False, indent=4, allow_nan=False)
        target_path = self._texts_root() / self._TEXT_DIRS[name] / f"{name}.json"
        async with self._texts_lock:  # 串行化：并发保存不会写出半成品
            await asyncio.to_thread(self._write_atomic, target_path, content)
        # 热更新运行中文案：workCopywriting/gameTexts -> 游戏文案；help -> 帮助长文本
        if name == "workCopywriting":
            self.ctx.set_copywriting({k: list(v) for k, v in data.items()})
        elif name == "gameTexts":
            # 与既有文案合并而非整体替换：gameTexts 只含决斗/排位赛键
            self.ctx.set_copywriting({**self.ctx.copy, **data})
        elif name == "help":
            await asyncio.to_thread(self.ctx.reload_texts)
        return _json({"ok": True, "keys": len(data)})
