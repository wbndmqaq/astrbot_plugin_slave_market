"""独立 Playwright 渲染器：Jinja2 模板 → HTML → Chromium 截图。

- 浏览器懒启动、全局复用（单例锁串行截图），启动与截图都有超时，绝不无限期挂住
- 截图保存到 plugin_data/screenshots/，按数量/体积/存活时长三重上限清理
- 只有浏览器真的断连才重建实例；普通渲染超时不拆浏览器，避免持续冷启动
- 任何失败返回 None，由调用方回退纯文本，绝不中断指令
"""

import asyncio
import contextlib
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

MAX_KEEP = 60  # 最多保留的截图数量
MAX_BYTES = 128 * 1024 * 1024  # 截图目录总体积上限
MIN_AGE = 120  # 秒：比这更新的图不清理（可能正在被适配器上传）
LAUNCH_TIMEOUT = 60  # 秒：Chromium 启动超时
SHOT_TIMEOUT = 45  # 秒：单次截图整体超时
CLOSE_TIMEOUT = 5  # 秒：关闭 page/browser 的单步上限
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")

# 渲染环境未就绪时的完整安装指引：整进程只打一次（见 _hint_once）。
# 之前"每次渲染失败各打一条 warning"，把真正有用的指引淹没了。
_ENV_HINT = (
    "渲染环境未就绪，所有指令已回退为纯文本。安装（约 1~2 分钟）：\n"
    "  1) pip install playwright\n"
    "  2) python -m playwright install chromium\n"
    "     （国内可加 PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/）\n"
    "  3) 仅 Linux/Docker 容器报 libnss3 / libnspr4 缺失时：\n"
    "     python -m playwright install-deps chromium\n"
    "  4) 重载插件（WebUI → 插件管理 → 本插件 → 重载）"
)
_hint_logged = False


class PlaywrightRenderer:
    def __init__(self, shot_dir: Path, scale: float = 2.0, logger=None):
        self.shot_dir = Path(shot_dir)
        # 与 _conf_schema.json 的 render_scale 上限保持一致：过高会让截图体积失控
        self.scale = max(1.0, min(4.0, float(scale)))
        self.log = logger
        self._pw = None
        self._browser = None
        self._ctx = None
        self._env = None
        self._tmpl_cache: OrderedDict[str, object] = OrderedDict()
        # 模板编译可能被多个 to_thread worker 并发调用（不同用户的指令
        # 各自跑一个线程池 worker），用 threading.Lock 保护 _env/_tmpl_cache，
        # 避免 check-then-act 竞态导致 OrderedDict 内部状态被并发破坏。
        self._tmpl_lock = threading.Lock()
        self._lock = asyncio.Lock()
        self._seq = 0
        self._closed = False  # close() 之后拒绝再拉起浏览器

    # ---------- 模板 ----------

    def render_template(self, template_str: str, data: dict) -> str:
        """同步方法：由调用方经 asyncio.to_thread 调用。编译结果按内容缓存。"""
        with self._tmpl_lock:
            if self._env is None:
                from jinja2 import Environment

                # 自动转义：昵称等用户可控内容不注入 HTML
                self._env = Environment(autoescape=True)
            tmpl = self._tmpl_cache.get(template_str)
            if tmpl is None:
                tmpl = self._env.from_string(template_str)
                self._tmpl_cache[template_str] = tmpl
                # LRU 替换最久未用的条目
                while len(self._tmpl_cache) > 64:
                    self._tmpl_cache.popitem(last=False)
        return tmpl.render(**data)

    # ---------- 截图 ----------

    def _hint_once(self) -> None:
        """整进程只打一次完整安装指引（环境未就绪时）。

        每次渲染失败都打一条普通 warning 会把真正有用的指引淹掉；
        README 里承诺的"首次失败输出一次完整指引"由这里兑现。
        """
        global _hint_logged
        if _hint_logged:
            return
        _hint_logged = True
        if self.log:
            self.log.error("[奴隶市场][Playwright] %s", _ENV_HINT)

    def _alive(self) -> bool:
        """浏览器与上下文都在，且连接未断。"""
        if self._browser is None or self._ctx is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:  # noqa: BLE001 - 探测失败即视为已断
            return False

    async def screenshot(self, html: str, name: str = "") -> str | None:
        if self._closed:
            return None  # 已卸载：不再拉起新浏览器，上层回退纯文本
        async with self._lock:
            if self._closed:
                return None
            try:
                return await asyncio.wait_for(
                    self._shot(html, name), timeout=SHOT_TIMEOUT
                )
            except Exception as e:  # noqa: BLE001 - 渲染失败交由上层回退文本
                if self.log:
                    self.log.warning(f"[奴隶市场][Playwright] 截图失败：{e}")
                # 只有浏览器确实断连才拆实例重建；普通超时保留浏览器，
                # 否则每次渲染超时都要重新冷启动 Chromium
                if not self._alive():
                    await self._teardown()
                return None

    async def _shot(self, html: str, name: str) -> str:
        if not self._alive():
            await self._teardown()
            await self._launch()
        page = await self._ctx.new_page()
        try:
            try:
                await page.set_content(html, wait_until="networkidle", timeout=15000)
            except Exception:  # noqa: BLE001 - 网络资源超时也照常出图
                await page.set_content(
                    html, wait_until="domcontentloaded", timeout=10000
                )
            self._seq += 1
            safe = _SAFE_NAME.sub("_", name)[:40] or "shot"
            out = self.shot_dir / f"{safe}_{self._seq}_{int(time.time())}.png"
            # 目录创建也不能跑在事件循环上
            await asyncio.to_thread(self.shot_dir.mkdir, parents=True, exist_ok=True)
            body = await page.query_selector("body")
            if body:
                await body.screenshot(path=str(out))
            else:
                await page.screenshot(path=str(out), full_page=True)
        finally:
            # 外层 wait_for 超时会取消本协程并等 finally 跑完；
            # 浏览器半死时 page.close() 可能挂很久，必须自带上限，
            # 否则渲染锁被一直占着，后续所有出图请求全部堆积
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.close(), timeout=CLOSE_TIMEOUT)
        await asyncio.to_thread(self._cleanup)
        return str(out)

    async def _launch(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            # playwright 未安装：这是"环境未就绪"的确定信号，打一次完整指引
            self._hint_once()
            raise RuntimeError(f"playwright 未安装：{e}") from e

        # 与 chromium.launch 配套的 driver 对象：先停 pw，Chromium 子进程才会全收；
        # 顺序不能反，否则 driver 死掉但子进程仍在。
        #
        # 「谁负责停 driver」的判据统一为 `self._pw is not None`：driver 句柄非空
        # 就说明它确实被 start 过（`start()` 抛异常时赋值不会发生），因此绝不会对
        # 一个未启动的对象调 stop()——原 `pw_started` 标志位的防护语义保留在这里。
        # 但它同时带来了一个 bug：复用路径（self._pw 来自上一次调用，本次没 start）
        # 下 launch 失败时不再回收 driver，半死 driver 被后续每次渲染复用，
        # 出图**永久**退化为纯文本。因此失败分支一律走完整的 _teardown(stop_pw=True)。
        browser = None
        try:
            if self._pw is None:
                self._pw = await async_playwright().start()
            # 用 Playwright 自带的 timeout 而不是外层 wait_for：
            # 外层取消可能正好落在 launch 返回之后、句柄赋值之前，
            # 那个 Chromium 进程就再没人能关，成为孤儿
            try:
                browser = await self._pw.chromium.launch(
                    headless=True, timeout=LAUNCH_TIMEOUT * 1000
                )
            except BaseException:
                # launch 自己抛了（超时/OOM/缺镜像）：连 browser 句柄都拿不到，
                # 必须清掉 driver；否则 pw 活着，没人 stop，下次 _launch 复用
                # 一个可能已坏的 driver，子进程累计泄漏
                self._hint_once()
                await self._teardown(stop_pw=True)
                raise
            try:
                ctx = await browser.new_context(
                    viewport={"width": 760, "height": 600},
                    device_scale_factor=self.scale,
                )
            except Exception:
                # 建 context 失败也要回收：先关浏览器（Playwright 会同步杀子进程），
                # 再停 driver，状态归零；_browser 不暴露给 _alive()。
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.close(), timeout=CLOSE_TIMEOUT)
                browser = None
                await self._teardown(stop_pw=True)
                raise
            self._browser = browser
            self._ctx = ctx
            if self.log:
                self.log.info("[奴隶市场][Playwright] 渲染器已就绪")
        except BaseException:
            # 兜底：上面任何一个 except 没接住的异常也走这里，绝不把
            # 半初始化的 self._pw / self._browser 留给下次 _launch 复用
            if browser is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.close(), timeout=CLOSE_TIMEOUT)
            await self._teardown(stop_pw=True)
            raise

    async def _teardown(self, stop_pw: bool = False):
        """关闭现有 context/browser（可选连 playwright 一起停），并置空句柄。

        每个 close 单步都自带 timeout：playwright 死锁时不能让 to_thread
        worker 永久挂住，否则 _launch 之后的指令全部排队。
        """
        for label, obj, meth in (
            ("ctx", self._ctx, "close"),
            ("browser", self._browser, "close"),
        ):
            if obj is None:
                continue
            try:
                await asyncio.wait_for(getattr(obj, meth)(), timeout=CLOSE_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                if self.log:
                    self.log.warning(f"[奴隶市场] renderer {label} 关闭异常：{e}")
        self._ctx = self._browser = None
        if stop_pw and self._pw is not None:
            try:
                await asyncio.wait_for(self._pw.stop(), timeout=CLOSE_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                # stop 失败可能是真泄漏（Chromium 进程残留），
                # 升级到 error 让运维看得到
                if self.log:
                    self.log.error(
                        f"[奴隶市场] renderer playwright 停止异常（可能存在孤儿进程）：{e}"
                    )
            # 即便 stop 抛了也置空：保留半死实例会让下次 _launch 复用
            self._pw = None

    def _cleanup(self):
        """同步清理：数量 + 总体积 + 存活时长三重约束，由 to_thread 调用。"""
        try:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            now = time.time()
            files = []
            for p in self.shot_dir.glob("*.png"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
            files.sort(key=lambda t: t[0], reverse=True)
            total = 0
            for i, (mtime, size, p) in enumerate(files):
                total += size
                too_many = i >= MAX_KEEP
                too_big = total > MAX_BYTES
                if (too_many or too_big) and now - mtime > MIN_AGE:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 关闭 ----------

    async def close(self):
        # 先置标志再抢锁：排在锁后面的请求拿到锁时会直接返回 None，
        # 不会在 terminate 之后又拉起一个没人负责关闭的 Chromium
        self._closed = True
        async with self._lock:
            await self._teardown(stop_pw=True)
