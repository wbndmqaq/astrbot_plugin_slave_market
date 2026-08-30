"""独立 Playwright 渲染器：Jinja2 模板 → HTML → Chromium 截图。

- 浏览器懒启动、全局复用（单例锁串行截图），启动与截图都有超时，绝不无限期挂住
- 截图保存到 plugin_data/screenshots/，按数量/体积/存活时长三重上限清理
- 只有浏览器真的断连才重建实例；普通渲染超时不拆浏览器，避免持续冷启动
- 任何失败返回 None，由调用方回退纯文本，绝不中断指令
"""

import asyncio
import contextlib
import re
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


class PlaywrightRenderer:
    def __init__(self, shot_dir: Path, scale: float = 2.0, logger=None):
        self.shot_dir = Path(shot_dir)
        self.scale = max(1.0, float(scale))
        self.log = logger
        self._pw = None
        self._browser = None
        self._ctx = None
        self._env = None
        self._tmpl_cache: "OrderedDict[str, object]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._seq = 0
        self._closed = False  # close() 之后拒绝再拉起浏览器

    # ---------- 模板 ----------

    def render_template(self, template_str: str, data: dict) -> str:
        """同步方法：由调用方经 asyncio.to_thread 调用。编译结果按内容缓存。"""
        if self._env is None:
            from jinja2 import Environment

            # 自动转义：昵称等用户可控内容不注入 HTML
            self._env = Environment(autoescape=True)
        tmpl = self._tmpl_cache.get(template_str)
        if tmpl is None:
            tmpl = self._env.from_string(template_str)
            self._tmpl_cache[template_str] = tmpl
            # LRU 替换最久未用的；不再 clear() 一把梭（高频模板反复重编译）
            while len(self._tmpl_cache) > 64:
                self._tmpl_cache.popitem(last=False)
        return tmpl.render(**data)

    # ---------- 截图 ----------

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
        from playwright.async_api import async_playwright

        # 与 chromium.launch 配套的 driver 对象：先停 pw，Chromium 子进程才会全收；
        # 顺序不能反，否则 driver 死掉但子进程仍在。`pw_started` 标志位让外层
        # 只清理"确实被 start 过的"实例，避免对一个未启动的 pw.stop() 抛异常。
        pw_started = False
        browser = None
        try:
            if self._pw is None:
                self._pw = await async_playwright().start()
                pw_started = True
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
                if pw_started and self._pw is not None:
                    try:
                        await asyncio.wait_for(self._pw.stop(), timeout=CLOSE_TIMEOUT)
                    except Exception:
                        pass
                    self._pw = None
                raise
            try:
                ctx = await browser.new_context(
                    viewport={"width": 760, "height": 600},
                    device_scale_factor=self.scale,
                )
            except Exception:
                # 建 context 失败也要回收：先关浏览器（Playwright 会同步杀子进程），
                # 再停 driver，状态归零。中间不再把 _browser 暴露给 _alive()。
                try:
                    await asyncio.wait_for(browser.close(), timeout=CLOSE_TIMEOUT)
                except Exception:
                    pass
                if pw_started and self._pw is not None:
                    try:
                        await asyncio.wait_for(self._pw.stop(), timeout=CLOSE_TIMEOUT)
                    except Exception:
                        pass
                self._pw = None
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
            if pw_started and self._pw is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._pw.stop(), timeout=CLOSE_TIMEOUT)
                self._pw = None
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
            # 即便 stop 抛了也置空：driver 不再可用，保留会让下次 _launch 复用半死实例
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
