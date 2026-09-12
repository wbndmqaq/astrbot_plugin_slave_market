"""鉴权中间件、登录限速与认证端点（拆分自原 webui/server.py）。

注意：`_guard` 是**类体内**用 `@web.middleware` 装饰的绑定方法（aiohttp 通过
函数对象上的 `__middleware_version__` 区分新旧式中间件，绑定方法会把它透传）。
拆分时必须保持这一形态——运行期再对绑定方法打一次 `web.middleware()` 会失败。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from urllib.parse import urlparse

from aiohttp import web

from ...core.auth import AuthError, rotate_password
from ._const import (
    COOKIE,
    CSRF_HEADER,
    LOGIN_FAILS_CAP,
    LOGIN_MAX_FAILS,
    LOGIN_WINDOW,
    PUBLIC_PATHS,
    TEMP_PASSWORD_FILE,
    _is_loopback_host,
    _json,
    _password_error,
)


class _AuthMixin:
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
            try:
                o = urlparse(origin)
            except ValueError:
                return False
            if o.scheme not in ("http", "https"):
                return False
            # Host 头必须先解析再比较：IPv6 字面量是 `[::1]:17818` 这种带方括号的
            # 形式，旧的 `req_host.split(":", 1)[0]` 会得到 "["，与 origin 的
            # "::1" 恒不相等 —— 于是用 http://[::1]:17818 打开面板时 GET 正常、
            # 登录与所有保存都 403。urlparse("//[::1]:17818").hostname == "::1"。
            try:
                h = urlparse("//" + request.headers.get("Host", ""))
                h_host, h_port = h.hostname, h.port
            except ValueError:
                return False
            if (o.hostname or "").lower() != (h_host or "").lower():
                return False
            try:
                o_port = o.port
            except ValueError:
                return False
            o_port = 80 if o_port is None else o_port
            h_port = (80 if o.scheme == "http" else 443) if h_port is None else h_port
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

    def _gc_fails(self, now: float) -> None:
        """清理限速记录并让容量上限真正生效。

        三类条目会在这里消失：
        1. 时间过期：最后一次失败已超出 LOGIN_WINDOW（**含已限速 IP**）——
           否则被限速的 IP 会永远留在字典里，攻击者可以用大量伪造 IP 做
           内存放大（旧实现注释里"限速 IP 跟随一次性涨到 ~2**32"就是这个问题）。
        2. 容量超限：仍超过 LOGIN_FAILS_CAP 时，优先淘汰"未达限速上限"的条目。
        3. 淘汰后仍超上限（全是限速 IP）时，按最后一次失败时间淘汰最旧的。
        """
        for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= LOGIN_WINDOW]:
            self._fails.pop(k, None)
        if len(self._fails) <= LOGIN_FAILS_CAP:
            return
        for k in sorted(self._fails, key=lambda k: (len(self._fails[k]), self._fails[k][-1])):
            if len(self._fails) <= LOGIN_FAILS_CAP:
                break
            if len(self._fails[k]) < LOGIN_MAX_FAILS:
                self._fails.pop(k, None)
        for k in sorted(self._fails, key=lambda k: self._fails[k][-1]):
            if len(self._fails) <= LOGIN_FAILS_CAP:
                break
            self._fails.pop(k, None)

    def _login_blocked(self, ip: str, now: float) -> bool:
        """返回 True 表示该 IP 当前在限速窗口内、应返回 429。

        维护策略：
        - 每次调用都先做一次容量/过期清理（**必须在"首见 IP 就 return"之前**：
          旧实现把清理放在早退之后，于是 `len(self._fails) > 1000` 的分支永远
          不可达，字典可以被无限撑大）。
        - 已限速 IP 不会被容量保护优先淘汰（避免"换个 IP 刷失败把别人从限速
          名单里挤出去"），但同样受窗口过期约束，不会永久驻留。
        """
        self._gc_fails(now)
        hits = [t for t in self._fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if not hits:
            self._fails.pop(ip, None)
            return False
        self._fails[ip] = hits
        return len(hits) >= LOGIN_MAX_FAILS

    def _client_ip(self, request) -> str:
        """登录限速用的客户端 IP。

        只有在直连对端就是本机（环回）时才采信 X-Forwarded-For：这覆盖
        "本机 nginx/caddy/frp 反代 + 监听 0.0.0.0 或 127.0.0.1"的部署，
        此时头由我们信任的本机代理追加。远程代理无法与"真实客户端伪造的
        XFF"区分，此时退化为按代理 IP 限速——否则任何客户端都能每个请求
        换一个伪造 IP，限速形同不存在；反过来也能把别人写进限速名单。

        取【最后一段】而不是第一段：nginx 最常见的写法是
        `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`，
        它把客户端自带的 XFF **原样保留在前**、再把真实对端**追加在末尾**。
        取第一段等于让客户端自己挑限速桶（每请求换一个随机值就能完全绕过
        登录限速），取最后一段才拿到反代看到的真实对端。
        """
        if _is_loopback_host(request.remote or ""):
            xff = request.headers.get("X-Forwarded-For", "")
            hops = [h.strip() for h in xff.split(",") if h.strip()] if xff else []
            if hops:
                return hops[-1][:64]
        return str(request.remote or "?")[:64]

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
            # 无密码模式（仅本机监听 + 写操作带自定义头）下鉴权完全放行：
            # 此时不会签发 JWT，若仍强行要求 token，前端所有需要鉴权的
            # 接口（/api/overview、/api/groups 等）都会拿到 401，面板整体不可用。
            need_auth = path not in PUBLIC_PATHS and self.auth_on
            if need_auth and not await self._authed_token(request):
                return self._unauth()
            if need_auth and self._must_reset() and path not in (
                "/api/auth/change-password",
                "/api/auth/logout",
                "/api/auth/check",
                "/api/meta",
            ):
                # 强制改密期间：除 change-password / logout / meta / check 外，
                # 全部以 423 拒绝，前端用它触发 reset overlay
                return _json(
                    {"error": "首次登录必须重置密码", "must_reset": True},
                    423,
                )
        return await handler(request)
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
        ip = self._client_ip(request)
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
            except Exception:  # noqa: BLE001, S110 - 重哈希失败不影响本次登录
                pass
        if not ok:
            self._fails.setdefault(ip, []).append(now)
            self.log.warning("[奴隶市场] WebUI 登录失败（%s）", ip)
            await asyncio.sleep(0.5)
            return _json({"error": "密码错误"}, 401)
        # 校验通过：先清限速记录，再发 JWT + 服务端会话
        self._fails.pop(ip, None)
        ttl = self._ttl()
        token, jti, _exp = self._issuer.issue(sub="admin", ttl_seconds=ttl)
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
        except Exception:  # noqa: BLE001, S110 - 清理失败不影响登录
            pass
        resp = _json(
            {
                "ok": True,
                "must_reset": bool(st.get("must_reset")),
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
        if not self.auth_on:
            return _json(
                {
                    "required": False,
                    "ok": True,
                    "must_reset": False,
                }
            )
        payload = await self._authed_token(request)
        if payload is None:
            # 未登录时**不得**回传 must_reset / show_password_plain：
            # /api/auth/check 在 PUBLIC_PATHS 白名单里（无需鉴权），
            # 这两个字段会泄漏"是否处于强制改密状态""是否开启明文回显"，
            # 给攻击者提供"面板刚初始化 / 密码可回显"的线索。恒返回 false。
            return _json({"required": True, "ok": False, "must_reset": False})
        return _json(
            {
                "required": True,
                "ok": True,
                "must_reset": self._must_reset(),
                "show_password_plain": bool(

                        self.ctx.config.get("webui_show_password_plain")
                        if self.ctx.config
                        else False

                ),
            }
        )

    async def _rotate_and_revoke(
        self, plaintext: str, cur_jti: str | None, ip: str, ua: str
    ) -> int:
        """改密的**唯一**实现：轮换哈希 → 同步配置种子 → 吊销既有会话 → 回写当前会话。

        改密必须吊销既有会话（含配置面板改密这条路径）：密码失窃后改密是标准
        补救动作，如果偷到 cookie 的人手里的 JWT 还能用，补救就是无效的。
        （历史上 `_change_password` 做了 revoke_all，配置面板的改密却没做。）

        必须同时把新明文写回配置：配置里的 `webui_password` 是磁盘哈希的
        **种子** —— `main._bootstrap_admin_password` 启动时会拿它与磁盘哈希比对、
        不一致就按配置重置。改密后不回写的话，任何一次 WebUI 改密（面板改密 /
        change-password / 首次强制改密）都会在下次重载插件时被静默回滚成配置里
        的旧明文：新密码失效、旧密码复活，而界面与日志都宣称"密码已更新"。

        返回被吊销的会话数。调用方负责长度校验与错误呈现。
        """
        await asyncio.to_thread(
            rotate_password,
            self._pwd_store,
            self._hasher,
            plaintext,
            must_reset=False,
        )
        await asyncio.to_thread(self._sync_config_password, plaintext)
        n = await asyncio.to_thread(self._sessions.revoke_all)
        if cur_jti:
            # 当前会话的 JWT 仍然有效（签名+exp 都没变），把会话表里的行放回去
            await asyncio.to_thread(
                self._sessions.put, cur_jti, "admin", ip, ua, ttl=self._ttl()
            )
        return n

    def _sync_config_password(self, plaintext: str) -> None:
        """把新明文写回插件配置（同步函数，调用方负责入线程）。

        写不进去（配置对象不支持保存）不算失败：只影响"下次重载是否回滚"，
        所以记一条 warning 让运维知道需要手工同步配置项。**两个分支都要记**：
        只用 `save_config` 是否存在来判"写没写进去"会漏掉"内存视图改了、磁盘
        没落盘"这种最隐蔽的形态——下次重载密码被静默回滚，日志里却什么都没有。
        """
        cfg = getattr(self.ctx, "config", None)
        if cfg is None or not hasattr(cfg, "__setitem__"):
            self.log.warning(
                "[奴隶市场] 新密码未能写回插件配置（当前配置对象不可写）；"
                "下次重载插件会按配置里的 webui_password 重置密码，请手工同步该项"
            )
            return
        try:
            cfg["webui_password"] = plaintext
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
            else:
                # 与 _admin_config_save 的 note 同口径
                self.log.warning(
                    "[奴隶市场] 新密码已写入配置内存视图，但当前配置对象不支持"
                    "持久化（无 save_config）；重载插件后会恢复原值，请手工同步"
                    "配置项 webui_password"
                )
        except Exception as e:  # noqa: BLE001 - 同步失败不阻断改密本身
            self.log.warning(
                f"[奴隶市场] 新密码未能写回插件配置（{e}）；"
                "下次重载插件会按配置里的 webui_password 重置密码，请手工同步该项"
            )

    async def _change_password(self, request):
        """重置管理员密码。要求已登录；JSON {old_password?, new_password}。

        - old_password：常规改密时必填；首次改密（must_reset=True）可省略
          （因为登录时已经用旧密码过过一次，本次就当切换）。
        - new_password：6~128 字节，UTF-8 编码后计算长度。
        - 改密成功 -> rotate_password 写入新哈希 + 清 must_reset；
          并由 `_rotate_and_revoke` 撤销其它设备 / 标签页的会话。
        """
        body, err = await self._body(request)
        if err:
            return err
        new_pwd = str(body.get("new_password", ""))
        old_pwd = str(body.get("old_password", ""))
        st = self._pwd_store.get() or {}
        must_reset = bool(st.get("must_reset"))
        # 校验 old：must_reset=True 时跳过（前端流程已经登录过一次了）
        old_ok = must_reset or (
            bool(old_pwd)
            and await asyncio.to_thread(self._hasher.verify, st.get("hash", ""), old_pwd)
        )
        if not old_ok:
            return _json({"error": "旧密码错误"}, 401)
        bad = _password_error(new_pwd)
        if bad:
            return _json({"error": bad}, 400)
        try:
            payload = await self._authed_token(request) or {}
            n = await self._rotate_and_revoke(
                new_pwd,
                payload.get("jti"),
                self._client_ip(request),
                request.headers.get("User-Agent", ""),
            )
        except AuthError as e:
            return _json({"error": f"密码哈希失败：{e}"}, 400)
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
                    except Exception:  # noqa: BLE001, S110 - 覆盖失败也继续删
                        pass
                    path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001, S110 - 任何清理失败都不阻断登录
                pass

        await asyncio.to_thread(_do)
