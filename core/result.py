"""统一的指令返回结构。"""


def R(
    tmpl: str | None = None,
    data: dict | None = None,
    text: str = "",
    err: str | None = None,
    img: str | None = None,
) -> dict:
    """构造一条指令结果。

    优先级：err > img > tmpl+data（Playwright 渲染） > text（纯文本回退）。
    """
    return {"err": err, "tmpl": tmpl, "data": data or {}, "text": text, "img": img}


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
