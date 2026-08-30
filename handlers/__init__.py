"""handlers 包：声明式指令路由（按业务域拆分）。"""

from .base import Route, install
from . import (
    bank_cmds,
    battle_cmds,
    market_cmds,
    social_cmds,
    system_cmds,
)

ALL_ROUTES: list[Route] = [
    *system_cmds.ROUTES,
    *market_cmds.ROUTES,
    *social_cmds.ROUTES,
    *battle_cmds.ROUTES,
    *bank_cmds.ROUTES,
]

__all__ = ["ALL_ROUTES", "Route", "install"]
