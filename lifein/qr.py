"""二维码渲染 —— 部署时"把一串东西弄进手机"的唯一体面办法。

两个调用方:微信扫码登录(ADR-018),以及 App 的设备配码
([06 §6.1](../docs/06-data-model.md#61-两组接口两套凭据))。后者要往手机里
搬两把 base64 密钥,手打是不现实的。

两种输出都保留,因为它们的失效方式不重叠:

- **HTML 文件**:不依赖任何终端能力。中文 Windows 的控制台是 GBK,
  画二维码用的方块字符直接编不出来 —— 那时只有这条路走得通
- **终端字符画**:不依赖浏览器。服务器上打开一个本地 HTML 文件是做不到的

用 SVG 而不是 PNG:PNG 要 Pillow,SVG 不用 —— 少一个依赖(ADR-017)。
"""

from __future__ import annotations

import io
from pathlib import Path


def render_qr_svg(data: str) -> str:
    """把 `data` 编成一段 SVG,**原样嵌进页面**。

    单独一个函数是给 Web 控制台用的:那边要把二维码**内联**进 HTML,
    而不是写成文件再 `<img src>` 引一次 —— 控制台里没有任何外部引用,
    多一个外链就多一处 Referer 会带着东西出去的地方(06 §6.13)。
    """
    import qrcode as qrcode_lib
    import qrcode.image.svg as qrcode_svg

    image = qrcode_lib.make(data, image_factory=qrcode_svg.SvgPathImage, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode()

    # **去掉 XML 声明。** `<?xml …?>` 只在文件开头合法,嵌进 HTML 里之后
    # 它会被当成一段文本原样显示在二维码上面 —— 而那看起来像页面坏了
    if svg.startswith("<?xml"):
        svg = svg[svg.index("?>") + 2 :].lstrip()
    return svg


def write_qr_html(data: str, path: Path, *, title: str, hint: str = "") -> None:
    """把 `data` 编成二维码写进一个 HTML 文件,双击就能用浏览器打开扫。

    `data` 原样进二维码,也原样显示在页面上 —— 扫不动的时候还能手抄。
    """
    svg = render_qr_svg(data)

    path.write_text(
        "<!doctype html><meta charset='utf-8'>"
        f"<title>LifeIn · {title}</title>"
        "<style>body{font-family:system-ui;text-align:center;padding:40px}"
        "svg{width:320px;height:320px}"
        "code{word-break:break-all;font-size:13px;color:#555;display:block;"
        "max-width:640px;margin:12px auto}</style>"
        f"<h2>{title}</h2>"
        f"{svg}"
        f"<code>{_escape(data)}</code>"
        f"<p style='color:#888;font-size:13px'>{hint}</p>",
        encoding="utf-8",
    )


def render_qr_ascii(data: str) -> str | None:
    """渲染成终端字符画。装不了依赖就返回 None,由调用方退回打文字。"""
    try:
        import qrcode as qrcode_lib
    except ImportError:
        return None

    code = qrcode_lib.QRCode(border=1)
    code.add_data(data)
    code.make(fit=True)
    buffer = io.StringIO()
    code.print_ascii(out=buffer, invert=True)
    return buffer.getvalue()


def _escape(text: str) -> str:
    """页面上要原样显示密钥,而密钥里可能有任何字符。

    这份 HTML 只在本机打开,但"反正没人看"正是注入类问题的标准开场白。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
