"""长文本与游戏文案加载器：内置 `resources/` + 用户覆盖 `data_root/overrides/`。

- 帮助等长文案从 JSON 读取，改文案不用动代码
- 全量载入内存，WebUI 保存后可热更新（force=True）
- **用户改动一律写入 `ctx.data_root/overrides/`，绝不写插件自身目录**：
  容器 / 插件市场里 `resources/` 常是只读挂载（保存直接 500，用户无从判断），
  而且插件升级会覆盖 `resources/`，把 WebUI 的编辑成果全部吞掉。
- 读取时按「用户覆盖 > 内置默认」合并，所以用户看到的永远是生效后的内容。

目录布局（`_OVERRIDE_DIR` 下的结构镜像 `resources/`）：

    data/plugin_data/<插件名>/overrides/data/workCopywriting.json
    data/plugin_data/<插件名>/overrides/data/gameTexts.json
    data/plugin_data/<插件名>/overrides/texts/help.json
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from astrbot.api import logger

# 插件根目录与内置资源目录（core/texts.py -> core -> 插件根）
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def find_plugin_file(name: str) -> Path | None:
    """在插件根目录下找文件（从本文件向上逐级探测）。

    **拆分后的子包必须用这个函数而不是写死 `parent.parent`**：
    `webui/server/_admin.py` 的 `parent.parent` 是 `webui/`，不是插件根目录，
    会导致 `_conf_schema.json` 读不到、配置面板静默失效（表现为改配置无任何反应）。
    以 `.pyc` 形式发布或目录被裁剪时返回 None，由调用方兜底。
    """
    for parent in Path(__file__).resolve().parents:
        cand = parent / name
        try:
            if cand.is_file():
                return cand
        except OSError:  # pragma: no cover - 权限/长路径等异常一律当作找不到
            continue
    return None
BUILTIN_RESOURCES = PLUGIN_ROOT / "resources"
# 用户覆盖目录名（位于 ctx.data_root 下）
OVERRIDE_DIR = "overrides"

# ---- 文案文件登记表：WebUI 与加载器共用同一份定义 ----
# 名称 -> resources/ 下的子目录（"data" = 游戏文案，必须按 key 直接下标访问；
# "texts" = 长文本，缺失时回退内置兜底）
TEXT_FILES: dict[str, str] = {
    "workCopywriting": "data",
    "gameTexts": "data",
    "help": "texts",
}

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

# 游戏文案的内置兜底：resources/data/*.json 缺失或损坏时用它保证指令不崩。
# 注意这只是"最小可运行集合"，文案正本在 resources/data/ 下；两处必须语义一致，
# 改默认文案请优先改 resources/data/ 里的 JSON。
_DEFAULT_COPYWRITING: dict[str, dict] = {
    "workCopywriting.json": {
        "slaveowner": ["靠着家族的资助，获得收入"],
        "success": ["搬了一天的砖，获得收入"],
        "failure": ["摸鱼被抓了个正着，一分没挣着,[A]身价下降[C]->[D]"],
        "expenses": ["你为奴隶购买了新饰品，花费了15金币。"],
        "buyMaster": ["对不起，人家是尊贵的大奴隶主，不可以购买捏~"],
    },
    "gameTexts.json": {
        "arena_actions": ["使出浑身解数"],
        "ranking_opponents": [
            {"name": "流浪剑客", "score": 800, "specialEffect": "剑术精湛，容易造成暴击"}
        ],
        "ranking_events": [{"name": "天气晴朗", "effect": 1.1, "desc": "状态绝佳"}],
        "ranking_tiers": [[1000, "青铜"]],
        "ranking_top_tier": "钻石",
    },
}


def override_root(data_root: Path) -> Path:
    """用户覆盖目录根：`<data_root>/overrides`。"""
    return Path(data_root) / OVERRIDE_DIR


def builtin_path(name: str) -> Path:
    """内置文案文件路径（只读）。"""
    return BUILTIN_RESOURCES / TEXT_FILES[name] / f"{name}.json"


def override_path(data_root: Path, name: str) -> Path:
    """用户覆盖文件路径（WebUI 写这里）。"""
    return override_root(data_root) / TEXT_FILES[name] / f"{name}.json"


def read_json(path: Path) -> dict | None:
    """读 JSON 对象；文件缺失/损坏/不是对象时返回 None（由调用方决定兜底）。"""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pick(value, fallback):
    """取有效值：类型与兜底一致且非空才算有效，否则用兜底。

    兜底为 None = 「最小兜底里没登记这个键」（正本里新增的键）：此时以 value
    自己的类型为准，非空 str/list 都直接采用。没有这一支，正本新增的**字符串**
    键会被解析成 None 而悄悄丢内容（`ranking_top_tier` 就是字符串键）。
    """
    if isinstance(fallback, str):
        return value if isinstance(value, str) and value else fallback
    if fallback is None:
        return value if isinstance(value, (str, list)) and value else None
    return value if isinstance(value, list) and value else fallback


def effective_text(data_root: Path, name: str) -> dict | None:
    """合并后的文案内容：内置默认 < 用户覆盖。两边都读不到时返回 None。

    WebUI 文案编辑器读到的是这里的返回值（用户看到的即实际生效的），
    写回则只写 override_path()。
    """
    base = read_json(builtin_path(name))
    over = read_json(override_path(data_root, name))
    if base is None and over is None:
        return None
    return {**(base or {}), **(over or {})}


def load_copywriting(data_root: Path) -> dict:
    """加载游戏文案：内置 resources/data/*.json，用户覆盖优先。

    三级回退，每一级都用「次优来源」接住上一级：
        用户覆盖 → 内置正本 → _DEFAULT_COPYWRITING 里的最小兜底

    三个容易踩的坑（都曾真实存在）：
    - 键集合必须取「内置正本 ∪ 最小兜底 ∪ **用户覆盖**」：只遍历兜底会让正本里
      新增、而兜底没登记的键凭空消失；漏掉用户覆盖则会让「只在覆盖里存在的键」
      出现编辑器看得见（WebUI 读的是 effective_text = 正本 ∪ 覆盖）、
      保存成功、重载后却不生效的静默不一致；
    - 用户覆盖类型不符/为空时必须退到**内置正本**而不是最小兜底 —— 兜底只有
      1 条样例，退到它等于把正本的 168 条文案静默缩成 1 条。

    同步方法：由调用方经 asyncio.to_thread 调用。
    """
    merged: dict = {}
    ignored: list[str] = []
    for fname, fallback in _DEFAULT_COPYWRITING.items():
        name = fname[: -len(".json")]
        base = read_json(builtin_path(name)) or {}
        over = read_json(override_path(data_root, name)) or {}
        for k in set(base) | set(fallback) | set(over):
            default = fallback.get(k)  # 最小兜底：类型的权威来源
            builtin = _pick(base.get(k), default)
            o = over.get(k)
            merged[k] = _pick(o, builtin)
            if o is not None and merged[k] is builtin and o is not builtin:
                ignored.append(f"{name}.{k}")
    if ignored:
        logger.warning(
            "[奴隶市场] 以下用户覆盖文案的类型不符或为空，已回退到内置文案："
            + "、".join(sorted(ignored))
        )
    return merged


class Texts:
    """长文本（help 等）：内置 + 用户覆盖两段式加载。"""

    def __init__(self, data_root: Path, builtin_root: Path | None = None):
        self.data_root = Path(data_root)
        self.builtin_dir = Path(builtin_root) if builtin_root else (
            BUILTIN_RESOURCES / "texts"
        )
        self.user_dir = override_root(self.data_root) / "texts"
        self._help: dict = copy.deepcopy(_DEFAULT_HELP)

    # ---------- 路径 ----------

    def builtin_path(self, name: str) -> Path:
        return self.builtin_dir / f"{name}.json"

    def user_path(self, name: str) -> Path:
        return self.user_dir / f"{name}.json"

    def effective(self, name: str) -> dict | None:
        """合并后的内容：内置默认 < 用户覆盖。两边都读不到时返回 None。

        用 self.builtin_path/self.user_path 而不是模块级 effective_text：
        后者写死了插件内置目录，构造时传了 builtin_root（测试/多实例）就两个
        入口读不同来源，属静默不一致。

        **只服务 texts/ 下的长文本**：self.builtin_dir 固定是
        `resources/texts/`，拿 `gameTexts`（在 data/ 下）来查会去
        `resources/texts/gameTexts.json` 找一个不存在的文件，于是静默返回
        None 或只返回用户覆盖——看起来"能跑"，实际读错了目录、拿不到正本。
        data/ 类文案请用模块级 effective_text()/load_copywriting()。
        """
        if name not in TEXT_FILES or TEXT_FILES[name] != "texts":
            raise ValueError(
                f"Texts.effective 只支持 texts/ 下的长文本，收到 {name!r}；"
                "data/ 类游戏文案请用 core.texts.effective_text/load_copywriting"
            )
        base = read_json(self.builtin_path(name))
        over = read_json(self.user_path(name))
        if base is None and over is None:
            return None
        return {**(base or {}), **(over or {})}

    # ---------- 加载 ----------

    def load_all(self, force: bool = False) -> None:
        """同步方法：由调用方经 asyncio.to_thread 调用。"""
        data = self.effective("help")
        if data is None:
            if force:
                logger.warning("[奴隶市场] help.json 加载失败，沿用现有内容")
            else:
                logger.warning("[奴隶市场] help.json 加载失败，使用内置兜底")
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
