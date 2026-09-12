"""统一的指令返回结构。"""


def R(
    tmpl: str | None = None,
    data: dict | None = None,
    text: str = "",
    err: str | None = None,
) -> dict:
    """构造一条指令结果。

    优先级：err > tmpl+data（Playwright 渲染） > text（纯文本回退）。

    说明：原有一个 `img` 字段（"直接给图片路径，不经模板渲染"），但全仓
    `grep img=` 命中 0 —— 没有任何生产方，`handlers/base.py` 里对应的分支
    属于死代码，故一并删除。需要直接发图时请走 tmpl+data 渲染管线。
    """
    return {"err": err, "tmpl": tmpl, "data": data or {}, "text": text}


def notice(
    icon: str, title: str, lines: list[str], tone: str = "ok", text: str = ""
) -> dict:
    """通用消息卡片（notice 模板）：错误/提示/简单结算一律走 HTML 渲染。"""
    fallback = title + (("\n" + "\n".join(lines)) if lines else "")
    return R(
        tmpl="notice",
        data={"icon": icon, "title": title, "lines": lines, "tone": tone},
        text=text or fallback,
    )
