"""独立端口 WebUI 管理面板（facade）：`WebUIServer` 由各域 Mixin 组合。

拆分自原单体 `webui/server.py`（1400+ 行），对外行为与 import 路径完全不变：

    from webui.server import WebUIServer          # 仍然可用
    from astrbot_plugin_slave_market.webui.server import WebUIServer, TEMP_PASSWORD_FILE

模块划分（各 Mixin 之间没有跨节私有状态，组合后共享 ctx / 锁 / 限速表）：

    _const.py   常量、_json/_json_threaded、_is_loopback_host、全局错误中间件
    _core.py    __init__ 与认证工具（JWT/cookie/密码存储装配）
    _auth.py    鉴权中间件 _guard、Host/CSRF 校验、登录限速与认证端点
    _serve.py   _body 解析、aiohttp 应用装配（_build_app）、start/stop、静态文件
    _api.py     公开元信息、只读查询 API、玩家与备份管理 API
    _admin.py   插件配置读写 + 文案编辑（写入 data_root/overrides/）

**aiohttp 中间件约束**：`_guard` 必须在类体内用 `@web.middleware` 装饰
（`_serve.py` 里以 `self._guard` 传入 `web.Application(middlewares=[...])`）。
aiohttp 用函数对象上的 `__middleware_version__` 区分新旧式中间件，绑定方法会
把它透传；若改成运行期对绑定方法再打一次 `web.middleware()`，aiohttp 会按旧式
工厂用 `(app, handler)` 调用 `_guard`，整个面板直接不可用。
`tests/test_imports.py::test_webui_middleware_is_new_style` 用真实请求守住这一点。
"""

from __future__ import annotations

from ._admin import _AdminMixin as _AdminMixin
from ._api import _ApiMixin as _ApiMixin
from ._auth import _AuthMixin as _AuthMixin
from ._const import (
    COOKIE as COOKIE,
)
from ._const import (
    CSRF_HEADER as CSRF_HEADER,
)
from ._const import (
    LOGIN_FAILS_CAP as LOGIN_FAILS_CAP,
)
from ._const import (
    LOGIN_MAX_FAILS as LOGIN_MAX_FAILS,
)
from ._const import (
    LOGIN_WINDOW as LOGIN_WINDOW,
)
from ._const import (
    MAX_BODY_BYTES as MAX_BODY_BYTES,
)
from ._const import (
    PAGE_SIZE as PAGE_SIZE,
)
from ._const import (
    PLAYERS_ALL_CAP as PLAYERS_ALL_CAP,
)
from ._const import (
    PUBLIC_PATHS as PUBLIC_PATHS,
)
from ._const import (
    SEARCH_LIMIT as SEARCH_LIMIT,
)
from ._const import (
    TEMP_PASSWORD_FILE as TEMP_PASSWORD_FILE,
)
from ._const import (
    TTL_DEFAULT as TTL_DEFAULT,
)
from ._const import (
    _error_middleware as _error_middleware,
)
from ._const import (
    _is_loopback_host as _is_loopback_host,
)
from ._const import (
    _json as _json,
)
from ._const import (
    _json_threaded as _json_threaded,
)
from ._const import (
    _safe_int as _safe_int,
)
from ._core import _CoreMixin as _CoreMixin
from ._serve import _ServeMixin as _ServeMixin


class WebUIServer(
    _CoreMixin,
    _AuthMixin,
    _ServeMixin,
    _ApiMixin,
    _AdminMixin,
):
    """独立端口管理面板：aiohttp + JWT + 服务端会话 + 全量管理 API。"""


__all__ = ["TEMP_PASSWORD_FILE", "WebUIServer"]
