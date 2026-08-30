"""WebUI 认证子系统。

- Argon2id 哈希密码：默认 time_cost=3, memory_cost=64MiB, parallelism=2（OWASP 2024 基线）
- JWT（HS256）作为会话令牌，载荷只放 jti / sub / exp / iat，不放敏感数据
- 服务端会话表（SQLite）：记录 jti / subject / created_at / expires_at / ip，用于：
  * 撤销（logout 时删掉一条）
  * 全局强制下线（webui 重启 + 改 JWT secret 后旧会话天然失效）
  * 多端登录审计
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

try:
    import jwt
except ImportError:  # pragma: no cover
    jwt = None  # type: ignore[assignment]

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
except ImportError:  # pragma: no cover
    PasswordHasher = None  # type: ignore[assignment]
    VerifyMismatchError = Exception  # type: ignore[assignment, misc]
    InvalidHashError = Exception  # type: ignore[assignment, misc]


class AuthError(Exception):
    """认证失败（密码错、JWT 失效、会话被吊销）。"""


class AuthUnavailable(RuntimeError):
    """依赖缺失，无法初始化认证子系统。"""


class Argon2Hasher:
    """对密码做 Argon2id 哈希 + 校验。"""

    def __init__(self, time_cost: int = 3, memory_cost: int = 64 * 1024, parallelism: int = 2):
        if PasswordHasher is None:
            raise AuthUnavailable("argon2-cffi 未安装，无法执行密码哈希")
        self._time_cost = max(1, int(time_cost))
        self._memory_cost = max(8 * 1024, int(memory_cost))
        self._parallelism = max(1, int(parallelism))
        self._ph = PasswordHasher(
            time_cost=self._time_cost,
            memory_cost=self._memory_cost,
            parallelism=self._parallelism,
            hash_len=32,
        )

    def hash(self, plaintext: str) -> str:
        """生成 Argon2id 哈希串（含盐与参数）。明文不会落盘。"""
        if not isinstance(plaintext, str) or plaintext == "":
            raise AuthError("密码不能为空")
        # 不允许超过 1024 字节，超长密码很可能是 DoS 攻击
        if len(plaintext.encode("utf-8")) > 1024:
            raise AuthError("密码过长")
        return self._ph.hash(plaintext)

    def verify(self, stored_hash: str, plaintext: str) -> bool:
        """校验明文 vs 哈希。需要 verify，自动处理重哈希（参数升级）。"""
        if not stored_hash or not plaintext:
            return False
        try:
            self._ph.verify(stored_hash, plaintext)
        except (VerifyMismatchError, InvalidHashError):
            return False
        except Exception:  # noqa: BLE001 - 任何 argon2 内部错误都视为不通过
            return False
        return True

    def needs_rehash(self, stored_hash: str) -> bool:
        """参数升级后，旧的哈希需要重新生成。调用 verify 成功后建议检查。"""
        try:
            return self._ph.check_needs_rehash(stored_hash)
        except Exception:
            return False


class JWTIssuer:
    """JWT（HS256）签发与校验。

    secret 长度强制 >= 32 字节（256 bit），低于此长度的密钥直接拒绝启动。
    secret 重置后所有现存会话立刻失效（签名不匹配）。
    """

    def __init__(self, secret: str, issuer: str = "astrbot-slave-market"):
        if jwt is None:
            raise AuthUnavailable("PyJWT 未安装")
        if not isinstance(secret, bytes):
            secret = secret.encode("utf-8")
        if len(secret) < 32:
            raise AuthError("JWT secret 长度不足 32 字节")
        self._secret = secret
        self._issuer = issuer
        self._alg = "HS256"

    def issue(self, sub: str, ttl_seconds: int, jti: str | None = None) -> tuple[str, str, int]:
        """签发 JWT。返回 (token, jti, exp_ts)。"""
        now = int(time.time())
        exp = now + max(1, int(ttl_seconds))
        jti = jti or secrets.token_urlsafe(16)
        payload = {
            "iss": self._issuer,
            "sub": str(sub),
            "iat": now,
            "exp": exp,
            "jti": jti,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._alg)
        if isinstance(token, bytes):  # PyJWT < 2 returns bytes
            token = token.decode("utf-8")
        return token, jti, exp

    def verify(self, token: str) -> dict[str, Any]:
        """校验 JWT，返回 payload dict；签名失败 / 过期 / 算法不匹配都抛 AuthError。"""
        if not token:
            raise AuthError("空 token")
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._alg],
                options={
                    "require": ["exp", "iat", "sub", "jti"],
                    "verify_iss": True,
                },
                issuer=self._issuer,
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("会话已过期") from e
        except jwt.InvalidTokenError as e:
            raise AuthError(f"JWT 校验失败: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"JWT 异常: {e}") from e
        return payload


class SessionStore:
    """服务端会话表：SQLite 单文件 `webui_sessions.db`。

    表结构：
      CREATE TABLE sessions (
        jti        TEXT PRIMARY KEY,
        sub        TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        ip         TEXT DEFAULT '',
        user_agent TEXT DEFAULT ''
      );

    - 创建会话：插入一行
    - 验证会话：SELECT 匹配 jti 且 expires_at > now
    - 吊销会话：DELETE WHERE jti=?
    - 定期清理：DELETE WHERE expires_at < now - 7d（保留一周供审计）
    """

    def __init__(self, db_path: Path):
        if jwt is None:
            raise AuthUnavailable("PyJWT 未安装")
        self.path = Path(db_path)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        jti        TEXT PRIMARY KEY,
                        sub        TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        ip         TEXT DEFAULT '',
                        user_agent TEXT DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS sessions_expires
                        ON sessions(expires_at);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def put(self, jti: str, sub: str, ip: str, user_agent: str, ttl: int) -> int:
        """登记一条新会话。返回 expires_at 戳。"""
        now = int(time.time())
        exp = now + max(60, int(ttl))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(jti, sub, created_at, expires_at, ip, user_agent) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        jti,
                        str(sub),
                        now,
                        exp,
                        str(ip or "")[:64],
                        str(user_agent or "")[:256],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return exp

    def exists(self, jti: str) -> bool:
        """会话存在且未过期。"""
        if not jti:
            return False
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM sessions WHERE jti=? AND expires_at>?",
                    (jti, now),
                )
                return cur.fetchone() is not None
            finally:
                conn.close()

    def revoke(self, jti: str) -> bool:
        """删除指定会话。返回是否原本存在。"""
        if not jti:
            return False
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE jti=?", (jti,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def revoke_all(self, sub: str | None = None) -> int:
        """吊销全部或某 sub 的会话，重启+改密钥后调用确保没有残留。

        返回被吊销的行数。
        """
        with self._lock:
            conn = self._connect()
            try:
                if sub:
                    cur = conn.execute("DELETE FROM sessions WHERE sub=?", (str(sub),))
                else:
                    cur = conn.execute("DELETE FROM sessions")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def gc(self, retain_seconds: int = 7 * 86400) -> int:
        """清理过期超过 retain_seconds 的会话行。"""
        cutoff = int(time.time()) - retain_seconds
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE expires_at<?", (cutoff,))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT COUNT(*) FROM sessions")
                return int(cur.fetchone()[0])
            finally:
                conn.close()


class PasswordStore:
    """管理 admin 密码哈希与"首次临时密码 → 必须改密"标记。

    文件布局（JSON，可手工清空 + 重启回到首次临时密码状态）：

      {
        "hash":  "$argon2id$...",   # Argon2id 哈希串，明文永不落盘
        "must_reset": false,        # True 时 login 成功但拒绝真正进面板
        "created_at": 1717000000,   # 最近一次"设密/临时"时间，用于审计
        "rotated_at": 1717000000,   # 最近一次"主动改密"时间
        "version": 1                # 存储格式版本号
      }

    # 注意：临时明文密码本身**只在创建时写一次到 data_dir/admin_passwd.txt**，
    # 供管理员登录。文件在第一次成功登录后应被立即删除（或由清理任务回收）。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._cache: dict | None = None

    def load(self) -> dict | None:
        with self._lock:
            try:
                self._cache = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                self._cache = None
            return self._cache

    def save(self, state: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                tmp.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    "utf-8",
                )
                # 原子替换；Windows 上 os.replace 在跨分区/被 mmap 时偶发
                # 失败，落到 shutil.move 是更稳的备选
                try:
                    import os as _os

                    _os.replace(tmp, self.path)
                except OSError:
                    import shutil

                    shutil.move(str(tmp), str(self.path))
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            self._cache = state

    def get(self) -> dict | None:
        if self._cache is None:
            return self.load()
        return self._cache

    def ensure_has_password(self) -> dict:
        """确保已存在一条密码记录。存在就返回，否则抛错（由调用方生成临时密码）。"""
        st = self.get()
        if st and st.get("hash"):
            return st
        raise AuthError("尚未初始化密码")


def rotate_password(
    store: PasswordStore,
    hasher: Argon2Hasher,
    new_plaintext: str,
    *,
    must_reset: bool = False,
) -> dict:
    """重置密码：生成新哈希 + 更新元数据。"""
    new_hash = hasher.hash(new_plaintext)
    now = int(time.time())
    st = store.get() or {}
    st.update(
        {
            "hash": new_hash,
            "created_at": now,
            "rotated_at": st.get("rotated_at", now),
            "must_reset": bool(must_reset),
            "version": 1,
        }
    )
    store.save(st)
    return st


__all__ = [
    "AuthError",
    "AuthUnavailable",
    "Argon2Hasher",
    "JWTIssuer",
    "SessionStore",
    "PasswordStore",
    "rotate_password",
]
