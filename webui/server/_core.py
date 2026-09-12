"""WebUIServer 的 _CoreMixin：__init__ 与认证工具（拆分自原 webui/server.py）。"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from aiohttp import web

from ...core.auth import (
    Argon2Hasher,
    AuthError,
    AuthUnavailable,
    JWTIssuer,
    PasswordStore,
    SessionStore,
)
from ._const import COOKIE, TTL_DEFAULT, _json, _safe_int


class _CoreMixin:
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
        # Argon2 哈希器时间成本走 config（默认 3，OWASP 2024 基线）；
        # 脏值/越界经 _safe_int 落回默认并夹取，绝不在这里抛（否则整个
        # WebUIServer 构造失败，进而连累插件初始化）。
        time_cost = _safe_int(
            (self.ctx.config.get("webui_hash_time_cost") if self.ctx.config else 3),
            3,
            1,
            10,
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
        # 静态文件（index.html/style.css/app.js）与包目录同级：拆分后
        # __file__ 在 webui/server/ 下，必须回退一级到 webui/
        self.dir = Path(__file__).resolve().parent.parent
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
