"""请求体解析、aiohttp 应用装配与生命周期（拆分自原 webui/server.py）。"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from ...core.auth import rotate_password
from ._const import MAX_BODY_BYTES, _error_middleware, _json


class _ServeMixin:
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

    def _build_app(self) -> web.Application:
        """构造 aiohttp 应用（路由 + 中间件）。与 start() 分离以便测试直接挂载。"""
        app = web.Application(
            middlewares=[_error_middleware, self._guard],
            # 显式设上限（aiohttp 默认 1MiB，且超限返回 HTML 413）。
            # 文案保存可以带整份 workCopywriting，1MiB 略显紧张；4MiB 足够
            # 又能防止有人用超大 body 打满内存。超限由 _error_middleware
            # 转成 JSON 413（见那里的注释）。
            client_max_size=MAX_BODY_BYTES,
        )
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
        return app

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
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
        await self._runner.setup()
        # 端口占用(EADDRINUSE)做短重试——旧实例完全释放前有一小段失败窗口；
        # PermissionError(10013)=端口被系统保留或防火墙拦截，重试无意义。
        # 只构造一个 site 反复 start，避免把失败的 site 一个个挂在 runner 上。
        #
        # 不传 reuse_address=True：Windows 上 SO_REUSEADDR 的语义与 POSIX 不同，
        # 它允许**同机另一个进程绑定同一端口并接管新连接**——攻击者（或误启动的
        # 第二个实例）可以顶着同一端口投一个假登录页，窃取管理员明文密码。
        # 保持 aiohttp 默认（None）：Windows 自动 False（重复绑定被 WinError
        # 10048 拒绝），POSIX 自动 True（便于 TIME_WAIT 后快速重启）。
        site = web.TCPSite(self._runner, self.host, self.port)
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
                # 收尾窗口。15s 覆盖大多数请求（截图/DB 都不该超过这个数）
                # 又不会让卸载永远卡在异常 handler 上。
                await asyncio.wait_for(runner.cleanup(), timeout=15)
            except TimeoutError:
                # 仍有 handler 占用：强制返回，端口由系统回收
                # （in-flight handler 在 cleanup() 内部会被 cancel，但
                # db.transact 的 to_thread worker 不在 loop 上，cancel 不到，
                # 这里超时就别再等，让上层继续走 db.close 的 15s 兜底）
                # 但绝不能静默：否则运维只看到"端口已释放"，不知道有请求被中断。
                self.log.warning(
                    "[奴隶市场] WebUI 停止超时（15s），可能有请求被中断；"
                    "端口随后由系统回收，若反复出现请检查是否有卡住的 DB 操作"
                )

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
