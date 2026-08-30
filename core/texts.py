"""长文本加载器：resources/texts/*.json。

- 帮助等长文案从 JSON 读取，改文案不用动代码
- 全量载入内存，WebUI 保存后可热更新（force=True）
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from astrbot.api import logger

# help 数据的兜底结构（texts/help.json 缺失或损坏时使用）
# 注意：help property 返回内部 dict 的只读引用（渲染只读，不做深拷贝以免每次
# 出图都复制整棵树）；任何要【原地修改】help 数据的调用方必须先自行 deepcopy。
_DEFAULT_HELP = {
    "title": "奴隶市场 · 帮助",
    "sub": "购买群友当奴隶，让奴隶打工赚金币；身价越高卖价越贵",
    "text": "奴隶市场帮助",
    "sections": [
        {
            "icon": "🛒",
            "title": "市场",
            "items": ["购买奴隶 @群友", "奴隶市场", "奴隶身价排行榜", "奴隶资金排行榜"],
        },
        {"icon": "👤", "title": "个人", "items": ["我的奴隶", "打工", "赎身", "抢劫"]},
    ],
}


class Texts:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._help: dict = copy.deepcopy(_DEFAULT_HELP)

    def load_all(self, force: bool = False) -> None:
        """同步方法：由调用方经 asyncio.to_thread 调用。"""
        try:
            data = json.loads((self.root / "help.json").read_text("utf-8"))
        except (OSError, ValueError) as e:
            if force:
                logger.warning(f"[奴隶市场] help.json 加载失败，沿用现有内容：{e}")
                return
            logger.warning(f"[奴隶市场] help.json 加载失败，使用内置兜底：{e}")
            data = {}
        if not isinstance(data, dict) or not data:
            return
        merged = copy.deepcopy(_DEFAULT_HELP)
        merged.update(copy.deepcopy(data))
        sections = merged.get("sections")
        if not isinstance(sections, list) or not sections:
            merged["sections"] = copy.deepcopy(_DEFAULT_HELP["sections"])
        else:
            # 只保留结构合法的分栏（字典且带非空 items），坏项不进模板
            merged["sections"] = [
                sec
                for sec in sections
                if isinstance(sec, dict)
                and isinstance(sec.get("items"), list)
                and sec.get("items")
            ] or copy.deepcopy(_DEFAULT_HELP["sections"])
        self._help = merged

    @property
    def help(self) -> dict:
        return self._help
