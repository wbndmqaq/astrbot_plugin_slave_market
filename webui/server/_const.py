"""WebUI 常量、响应工具与全局错误中间件（拆分自原 webui/server.py）。

安全模型（同原模块 docstring）：
- 密码：仅以 Argon2id 哈希形式落盘（auth.PasswordStore），明文不出现在磁盘。
- 会话：JWT(HS256) 作为客户端 cookie，服务端维护 SQLite 会话表
  （auth.SessionStore），登出/换密/重启直接吊销。
- WebUI 默认启用：插件首次启动会生成临时随机密码，写到
  data_dir/admin_passwd.txt 与日志，**仅出现一次**。首次登录后会要求
  强制改密（must_reset=True），改密后临时文件被删除。
- CSRF / Origin 校验 / 同源比对 / 失败限速。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from urllib.parse import urlsplit

from aiohttp import web

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
# 限速记录表的容量硬上限：超出后优先淘汰"未达限速"的条目。
# 没有这个上限，伪造 IP 可以把字典无限撑大（旧实现的清理分支不可达）。
LOGIN_FAILS_CAP = 1000
# 请求体上限：文案保存可以带整份 workCopywriting（约 64KB），留足余量又必须有界。
# aiohttp 默认 1MiB 且超限时返回 HTML 413，前端 res.json() 解析失败只会显示
# "Request Entity Too Large"，用户不知道是自己的内容太大还是插件坏了。
MAX_BODY_BYTES = 4 * 1024 * 1024
# 密码长度策略（change-password 与配置面板改密共用同一套校验）
PWD_MIN_BYTES = 6
PWD_MAX_BYTES = 128
# /api/players_all 的全量返回上限（跨群累计）。参照 core/service.py 的
# FULL_SCAN_CAP：给免冷却选择器一个"足够用但一定有界"的规模，
# 避免前端每次打开配置面板都拉全库并让服务端 json.dumps 整个结果。
PLAYERS_ALL_CAP = 10000
# /api/search 单次返回上限
SEARCH_LIMIT = 20
_STR_MAX = 512  # 字符串配置单值最大长度
_LIST_MAX = 500  # 列表配置最大条目数
_TEXT_ITEMS_MAX = 2000  # 单个文案键最多条目数
_TEXT_LEN_MAX = 500  # 单条文案最大长度
_BAD = object()  # _cast 的失败哨兵（与 None 区分：None 可能是合法值）
# workCopywriting 必须存在且非空的键：缺一个就会让打工指令直接抛异常
_COPY_REQUIRED = ("slaveowner", "success", "failure", "expenses", "buyMaster")

# 临时密码文件名（启动一次后建议重命名/删除以免泄漏）
TEMP_PASSWORD_FILE = "admin_passwd.txt"  # noqa: S105 - 这是文件名，不是密码


def _response(text: str, status: int) -> web.Response:
    return web.Response(
        text=text,
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _json(obj, status=200):
    """小响应：在事件循环里直接 dumps（微秒级）。

    大 payload（玩家全量列表等）请用 `_json_threaded`，否则 json.dumps 会把
    事件循环占住数十~数百毫秒，拖停同进程内所有群消息处理。

    allow_nan=False：inf/NaN 会被 json.dumps 写成 Infinity，
    那是非法 JSON，浏览器 JSON.parse 直接失败导致整页打不开。
    """
    return _response(json.dumps(obj, ensure_ascii=False, allow_nan=False), status)


async def _json_threaded(obj, status=200):
    """大 payload 的 JSON 响应：序列化丢进 to_thread，不阻塞事件循环。"""
    text = await asyncio.to_thread(
        json.dumps, obj, ensure_ascii=False, allow_nan=False
    )
    return _response(text, status)


# ---- 纯工具（模块级，**不放 Mixin**）----
# 这三个函数被多个 Mixin 共用。写在某个 Mixin 里再被别人 `self._xxx` 调用，
# 等于让 `_admin` 静默依赖 `_api` / `_auth` 存在：MRO 一旦重排或那个 Mixin
# 被移除，配置保存整条路径直接 AttributeError（跨 Mixin 私有依赖是静态检查
# 也难发现的事故）。tests/test_split.py::test_no_cross_mixin_private_helpers
# 会拒绝这种写法。


def _finite(raw) -> float | None:
    """转 float；非法或非有限值（NaN/inf）返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _safe_int(raw, default: int, lo: int, hi: int) -> int:
    """把配置里的脏值安全转成 int 并夹到 [lo, hi]。

    配置文件（或 WebUI 表单）可能给出 ""/None/"abc"/inf 这类值，裸 `int(...)`
    会抛 ValueError / TypeError / OverflowError。这类强转点若落在插件**初始化**
    路径上（webui_port、webui_hash_time_cost、backupKeep、render_scale…），
    一处脏值就会让整个插件加载失败——也就是全部游戏指令一起失效。WebUI 及
    其配置项属于可选功能，必须按默认值降级。lo/hi 与 _conf_schema.json 的
    min/max 同口径，越界一律夹取而不是报错。
    """
    try:
        v = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, v))


def _index_arg(body: dict) -> int | None:
    """取备份序号（从 1 开始）；缺参/非法/小于 1 返回 None。"""
    try:
        n = int(body.get("index", 0))
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _password_error(new_pwd: str) -> str | None:
    """密码长度策略（change-password 与配置面板改密共用）。None = 通过。"""
    n = len(new_pwd.encode("utf-8")) if new_pwd else 0
    if n < PWD_MIN_BYTES or n > PWD_MAX_BYTES:
        return f"新密码长度需在 {PWD_MIN_BYTES}~{PWD_MAX_BYTES} 字节之间（UTF-8）"
    return None


def _is_loopback_host(host: str) -> bool:
    """判断 Host 头/对端地址是否指向本机（含 `host:port` 与 IPv6 字面量）。

    必须容得下 `[::1]:17818` 这种写法：早先的实现先 `strip("[]")` 再按
    「只有一个冒号才算 host:port」剥离端口，`[::1]:17818` 会被削成
    `::1]:17818`（三段冒号）→ 既不剥端口也不是合法 IP → 返回 False。
    后果是无密码模式下用 `http://[::1]:17818` 打开面板时页面能开、但**所有**
    /api 请求被 `_host_ok` 判成非本机而 403（静默失效）。这里统一走 URL 解析，
    与 _csrf_ok 的 Host 处理共用同一套口径。
    """
    raw = str(host or "").strip()
    if not raw:
        return False
    # urlparse 需要一个 scheme 才认 netloc；补上 "//" 让它按 host[:port] 解析
    try:
        h = urlsplit("//" + raw).hostname or ""
    except ValueError:
        h = ""
    if not h:
        # 退化路径：不是合法 host[:port]（例如纯 IPv6 "::1" 没带方括号）
        h = raw.split("%")[0].strip().strip("[]").lower()
        if h.count(":") == 1 and not h.startswith(":"):
            h = h.rsplit(":", 1)[0]
    h = h.lower()
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
    except web.HTTPRequestEntityTooLarge:
        # 请求体超过 client_max_size：返回 JSON 而不是 aiohttp 的 HTML 413，
        # 否则前端 res.json() 失败，只能显示 "Request Entity Too Large"
        return _json({"error": f"请求体过大，上限 {MAX_BODY_BYTES // 1024} KB"}, 413)
    except web.HTTPException:
        raise
    except Exception:
        srv = request.app.get("slvm_server")
        if srv is not None:
            srv.log.exception("[奴隶市场] WebUI 处理 %s 时出错", request.path)
        return _json({"error": "内部错误，请查看 AstrBot 日志"}, 500)
