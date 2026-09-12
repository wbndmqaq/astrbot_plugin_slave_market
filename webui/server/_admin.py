"""插件配置读写 + 文案编辑（拆分自原 webui/server.py）。

文案编辑的读写在 `ctx.data_root/overrides/` 下（绝不写插件自身目录），
读取时按「用户覆盖 > 内置默认」合并——见 core/texts.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path

from ...core.auth import AuthError
from ...core.texts import TEXT_FILES, effective_text, find_plugin_file, override_path
from ._const import (
    _BAD,
    _COPY_REQUIRED,
    _LIST_MAX,
    _STR_MAX,
    _TEXT_ITEMS_MAX,
    _TEXT_LEN_MAX,
    CONFIG_HIDDEN_KEYS,
    _finite,
    _is_loopback_host,
    _json,
    _json_threaded,
    _password_error,
)


class _AdminMixin:
    # ===== 插件配置 =====

    @staticmethod
    def _read_schema() -> dict:
        """读插件根目录下的 `_conf_schema.json`。

        路径查找必须走 find_plugin_file：本文件在 `webui/server/` 下，
        写死 `parent.parent` 会指到 `webui/`，schema 读空会让配置面板整体失效。
        """
        sp = find_plugin_file("_conf_schema.json")
        if sp is None:
            return {}
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
                v = _finite(raw)
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
        # 当前会话的 jti：配置面板改密同样要"只保留本次会话"
        cur_jti = (await self._authed_token(request) or {}).get("jti")
        ip = self._client_ip(request)
        notes: list[str] = []
        applied = 0
        # 仅当本请求真正"提交了非空密码并走 _rotate_and_revoke（改密的唯一实现：
        # 轮换哈希 → 回写配置种子 → 吊销既有会话）成功"时，
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
                # webui_password 走单独的"重哈希 + 吊销会话"通道：
                # 存的是明文，磁盘上是 Argon2id；长度策略与 /api/auth/change-password
                # 完全共用（_password_error），旧实现这里没有最小长度，面板可以把
                # 密码设成 1 个字符。
                if k == "webui_password" and v:
                    bad = _password_error(str(v))
                    if bad:
                        notes.append(f"管理员密码未更新：{bad}")
                        continue
                    try:
                        n = await self._rotate_and_revoke(
                            str(v), cur_jti, ip, request.headers.get("User-Agent", "")
                        )
                        notes.append(
                            f"管理员密码已更新（已用 Argon2id 重哈希，并吊销 {n} 条会话）"
                        )
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
            scale = _finite(target.get("render_scale"))
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

    # ===== 文案编辑（游戏文案 + 帮助长文本）=====

    # 文案文件名 -> resources/ 子目录的唯一来源在 core/texts.py（TEXT_FILES）；
    # WebUI 只允许编辑登记过的这三个库。
    _TEXT_DIRS = TEXT_FILES
    def _texts_data_root(self) -> Path:
        """用户覆盖目录的根 = 插件数据目录（**绝不写插件自身目录**）。"""
        return Path(self.ctx.data_root)

    def _texts_override_path(self, name: str) -> Path:
        """用户覆盖文件的写入目标：data_root/overrides/<data|texts>/<name>.json"""
        return override_path(self._texts_data_root(), name)

    async def _texts_get(self, request):
        name = request.query.get("name", "")
        if name not in self._TEXT_DIRS:
            return _json({"error": "bad name"}, 400)
        # 返回「内置默认 < 用户覆盖」合并后的生效内容，编辑器看到的即实际生效的
        data = await asyncio.to_thread(effective_text, self._texts_data_root(), name)
        if data is None:
            return _json({"error": "未找到"}, 404)
        return await _json_threaded({"name": name, "data": data})

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
            if not isinstance(data["sections"], list) or not data["sections"]:
                return "键 sections 至少要有一个分栏"
            for i, sec in enumerate(data["sections"], 1):
                if not isinstance(sec, dict) or not isinstance(sec.get("items"), list):
                    return f"第 {i} 个分栏结构非法"
                if not all(isinstance(x, str) for x in sec["items"]):
                    return f"第 {i} 个分栏的条目必须是字符串"
                # 以下两条与前端 collectHelp() 的拦截**同口径**（前端拦、后端放
                # 会让直接调 API / 旧版前端写入不可见的坏数据）：
                # - 空标题：Texts.load_all 只按 items 过滤，空标题分栏不会被丢掉，
                #   而是渲染成一张没有标题的空卡片，用户看到一栏空白；
                # - 空条目：load_all 会整栏丢弃（连同标题），静默保存 = 静默删数据。
                if not str(sec.get("title") or "").strip():
                    return f"第 {i} 个分栏缺少标题"
                if not sec["items"]:
                    return f"第 {i} 个分栏没有任何条目"
                if len(sec["items"]) > _TEXT_ITEMS_MAX:
                    return f"第 {i} 个分栏的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
                for x in sec["items"]:
                    if not x.strip():
                        return f"第 {i} 个分栏含空条目，请删除或填写内容"
                    if len(x) > _TEXT_LEN_MAX:
                        return f"第 {i} 个分栏有条目超过 {_TEXT_LEN_MAX} 字"
                if "icon" in sec and not isinstance(sec["icon"], str):
                    return f"第 {i} 个分栏的 icon 必须是字符串"
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
        for k in data:
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
            if len(v) > _TEXT_ITEMS_MAX:
                return f"键 ranking_opponents 的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
            for i, o in enumerate(v, 1):
                if not isinstance(o, dict):
                    return f"第 {i} 个对手结构非法"
                if not isinstance(o.get("name"), str) or not o["name"].strip():
                    return f"第 {i} 个对手缺少名称"
                # 名字/说明会直接渲染进结算卡片与排行榜：与 arena_actions 同口径限长
                if len(o["name"]) > _TEXT_LEN_MAX:
                    return f"第 {i} 个对手的名称超过 {_TEXT_LEN_MAX} 字"
                try:
                    int(o.get("score"))
                except (TypeError, ValueError):
                    return f"第 {i} 个对手的 score 必须是数字"
                if not isinstance(o.get("specialEffect"), str):
                    return f"第 {i} 个对手的 specialEffect 必须是字符串"
                if len(o["specialEffect"]) > _TEXT_LEN_MAX:
                    return f"第 {i} 个对手的 specialEffect 超过 {_TEXT_LEN_MAX} 字"
        if "ranking_events" in data:
            v = data["ranking_events"]
            if not isinstance(v, list) or not v:
                return "键 ranking_events 必须是非空数组"
            if len(v) > _TEXT_ITEMS_MAX:
                return f"键 ranking_events 的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
            for i, e in enumerate(v, 1):
                if not isinstance(e, dict):
                    return f"第 {i} 个事件结构非法"
                if not isinstance(e.get("name"), str) or not e["name"].strip():
                    return f"第 {i} 个事件缺少名称"
                if len(e["name"]) > _TEXT_LEN_MAX:
                    return f"第 {i} 个事件的名称超过 {_TEXT_LEN_MAX} 字"
                try:
                    float(e.get("effect"))
                except (TypeError, ValueError):
                    return f"第 {i} 个事件的 effect 必须是数字"
                if not isinstance(e.get("desc"), str):
                    return f"第 {i} 个事件的 desc 必须是字符串"
                if len(e["desc"]) > _TEXT_LEN_MAX:
                    return f"第 {i} 个事件的 desc 超过 {_TEXT_LEN_MAX} 字"
        if "ranking_tiers" in data:
            v = data["ranking_tiers"]
            if not isinstance(v, list) or not v:
                return "键 ranking_tiers 必须是非空数组"
            if len(v) > _TEXT_ITEMS_MAX:
                return f"键 ranking_tiers 的条目过多（上限 {_TEXT_ITEMS_MAX} 条）"
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
                # 段位名会渲染进排行榜卡片：限长与 arena_actions 一致
                if len(t[1]) > _TEXT_LEN_MAX:
                    return f"第 {i} 个段位的名称超过 {_TEXT_LEN_MAX} 字"
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
        # 校验与序列化都是用户可塞 ~4MB 内容的入口，全部丢线程池
        bad = await asyncio.to_thread(self._validate_texts, name, data)
        if bad:
            return _json({"error": bad}, 400)
        content = await asyncio.to_thread(
            json.dumps, data, ensure_ascii=False, indent=4, allow_nan=False
        )
        # 写到 data_root/overrides/ 而不是插件目录：容器/插件市场里 resources/
        # 常是只读挂载（写插件目录会直接 500），且插件升级会覆盖 resources/、
        # 把 WebUI 的编辑成果全部吞掉。.bak/.tmp 同样落在用户目录里。
        target_path = self._texts_override_path(name)
        async with self._texts_lock:  # 串行化：并发保存不会写出半成品
            await asyncio.to_thread(self._write_atomic, target_path, content)
        # 热更新运行中文案：workCopywriting/gameTexts -> 游戏文案；help -> 帮助长文本
        if name == "workCopywriting":
            # 与既有文案**合并**而非整体替换：ctx.copy 里还装着决斗/排位赛的
            # arena_actions / ranking_opponents / ranking_events / ranking_tiers /
            # ranking_top_tier。整体替换会让保存一次打工文案之后 `！决斗` 报
            # 「决斗动作文案未配置」、`！排位赛` 同样失效，直到重载插件。
            self.ctx.set_copywriting({**self.ctx.copy, **data})
        elif name == "gameTexts":
            # 与既有文案合并而非整体替换：gameTexts 只含决斗/排位赛键
            self.ctx.set_copywriting({**self.ctx.copy, **data})
        elif name == "help":
            await asyncio.to_thread(self.ctx.reload_texts)
        return _json({"ok": True, "keys": len(data)})
