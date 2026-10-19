"""托盘图标绘制。

两个必须遵守的约束：

1. **ICO 帧要用 BMP/DIB 而不是 PNG 压缩。** Pillow 默认把所有帧写成 PNG，
   而 pystray 是用 ``LoadImage(LR_LOADFROMFILE)`` 加载的，这条路径对 PNG 帧
   不可靠；失败时 ``hIcon`` 为 NULL，图标**静默不出现**，没有任何报错。
   BMP 帧比 PNG 帧只大十来 KB，不值得为这点体积冒险。

2. **多尺寸要逐尺寸重画**，不要把大图缩到 16×16。Pillow 的 ICO 写出逻辑对
   ``append_images`` 里尺寸精确匹配的图会直接采用，不会二次缩放。

16×16 下数字完全不可读，所以状态用**颜色 + 形状**双重编码（对色盲也友好）：
充足=实心圆点、偏低=实心圆加横杠、告急=实心圆加感叹号、出错=灰色空心环。

在此之上再叠一个正交的维度：**计费时段**。空闲时段用上面这套原图，高峰时段
在同样的底图上盖一道红色斜杠。两个维度互不干扰，所以组合数是 4×2=8 张图。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

log = logging.getLogger(__name__)

COLORS: dict[str, tuple[int, int, int, int]] = {
    "ok": (34, 197, 94, 255),        # 绿
    "warn": (234, 179, 8, 255),      # 黄
    "critical": (239, 68, 68, 255),  # 红
    "error": (128, 128, 128, 255),   # 灰
}
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)

#: 高峰斜杠。本体特意比 critical 的红更深一点，压在同色圆上也还分得出
SLASH = (200, 30, 30, 255)
#: 斜杠描边。**必须**有：critical 的底图本身就是红的，不描边的话红压红完全
#: 看不出来；而描边用深色而不是白色，是因为 critical 的感叹号是白的，白描边
#: 会和它糊在一起。深描边对绿/黄/红/灰四种底、以及深浅两种任务栏都能分开。
SLASH_CASING = (24, 24, 24, 255)

#: 托盘用。含 64 是为了高 DPI 下 LoadImage 请求更大的 SM_CXICON 时仍有余量
SIZES = (16, 20, 24, 32, 48, 64)
#: 资源管理器（--icon）用，多一个 256 让大图标视图清晰
APP_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

SUPERSAMPLE = 8


#: 斜杠超出圆形的余量（占 canvas 比例）。斜杠画得比圆长，多出来的部分靠圆形
#: 遮罩裁掉 —— 这样首尾正好切成两段圆弧，既不会伸进透明的四角留下悬空残端，
#: 也不用去手调一个「刚好够长又不露头」的角度（那个值随描边宽度会变，很脆）。
SLASH_OVERHANG = 0.02
#: 斜杠本体宽度
SLASH_WIDTH = 0.14
#: 含描边的总宽度
SLASH_CASING_WIDTH = 0.22


def _draw_slash(image: Image.Image, canvas: int, box: tuple[float, float, float, float]) -> None:
    """盖一道左上 -> 右下的红色斜杠，带深色描边（理由见 SLASH_CASING 注释）。

    尺寸全按 canvas 比例给，所以 16×16 和 64×64 上观感一致。16×16 是最难的：
    超采样后斜杠约 18 px 宽、描边每侧约 5 px，缩回实际尺寸后描边只剩 ~0.6 px，
    再细就在这里糊没了 —— 这几组数字是拿实际渲染图比对出来的，别随手调小。
    """
    low = canvas * SLASH_OVERHANG
    high = canvas - low

    layer = Image.new("RGBA", (canvas, canvas), TRANSPARENT)
    pen = ImageDraw.Draw(layer)
    pen.line([(low, low), (high, high)], fill=SLASH_CASING, width=int(canvas * SLASH_CASING_WIDTH))
    pen.line([(low, low), (high, high)], fill=SLASH, width=int(canvas * SLASH_WIDTH))

    # 按圆裁剪。error 档底图是空心环，遮罩仍取外圆 —— 斜杠横穿环心是对的，就是 🚫 的样子
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))

    image.alpha_composite(layer)


def render(state: str, size: int, peak: bool = False) -> Image.Image:
    """画单个尺寸的图标。先按 8 倍尺寸画再 LANCZOS 缩下来，边缘才平滑。

    ``peak=True`` 时在底图上盖红色斜杠（高峰时段）。
    """
    color = COLORS.get(state, COLORS["error"])
    canvas = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), TRANSPARENT)
    draw = ImageDraw.Draw(image)

    pad = canvas * 0.06
    box = (pad, pad, canvas - pad, canvas - pad)

    if state == "error":
        draw.ellipse(box, outline=color, width=max(2, int(canvas * 0.10)))
    else:
        draw.ellipse(box, fill=color)
        if state == "ok":
            radius = canvas * 0.15
            centre = canvas / 2
            draw.ellipse(
                [centre - radius, centre - radius, centre + radius, centre + radius], fill=WHITE
            )
        elif state == "warn":
            width, height = canvas * 0.50, canvas * 0.13
            draw.rounded_rectangle(
                [
                    canvas / 2 - width / 2, canvas / 2 - height / 2,
                    canvas / 2 + width / 2, canvas / 2 + height / 2,
                ],
                radius=height / 2, fill=WHITE,
            )
        elif state == "critical":
            width = canvas * 0.13
            top, height = canvas * 0.22, canvas * 0.40
            draw.rounded_rectangle(
                [
                    canvas / 2 - width / 2, top,
                    canvas / 2 + width / 2, top + height,
                ],
                radius=width / 2, fill=WHITE,
            )
            dot = canvas * 0.70
            draw.ellipse([canvas / 2 - width / 2, dot, canvas / 2 + width / 2, dot + width], fill=WHITE)

    if peak:
        _draw_slash(image, canvas, box)

    return image.resize((size, size), Image.LANCZOS)


def build_ico(
    state: str, sizes: tuple[int, ...], bitmap_format: str | None, peak: bool = False
) -> bytes:
    frames = [render(state, size, peak=peak) for size in sizes]
    largest = max(frames, key=lambda img: img.size[0])
    others = [img for img in frames if img is not largest]
    buffer = io.BytesIO()
    # Pillow 只接受 append_images 里尺寸精确匹配的帧，所以每张都按目标尺寸画好了
    largest.save(
        buffer,
        format="ICO",
        sizes=[(size, size) for size in sizes],
        append_images=others,
        **({"bitmap_format": bitmap_format} if bitmap_format else {}),
    )
    return buffer.getvalue()


def for_state(state: str, peak: bool = False) -> "IcoBlob":
    """取某个状态+时段对应的托盘图标对象（带缓存，每张只画一次）。"""
    key = (state, bool(peak))
    blob = _BLOB_CACHE.get(key)
    if blob is None:
        blob = IcoBlob(build_ico(state, SIZES, bitmap_format="bmp", peak=peak))
        _BLOB_CACHE[key] = blob
    return blob


_BLOB_CACHE: dict[tuple[str, bool], "IcoBlob"] = {}


class IcoBlob:
    """鸭子类型适配 pystray。

    pystray 通过 ``serialized_image()`` 调 ``image.save(f, format='ICO')`` 把图标
    落成临时文件，所以只要有个 ``save`` 方法就行 —— 这样比传 PIL Image 更可控，
    因为 PIL Image 无法携带 ``bitmap_format="bmp"`` 这个 encoderinfo。

    注意：**刻意不定义 ``__bool__`` / ``__len__``**。pystray 的 ``icon`` setter 里
    有 ``if value:`` 判断，定义成假值会让图标被当成"清空"处理。
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = data

    def save(self, fp, format=None) -> None:  # noqa: A002 - 签名要匹配 Pillow
        fp.write(self._data)

    def __repr__(self) -> str:
        return f"<IcoBlob {len(self._data)} bytes>"


def write_app_ico(path: Path) -> None:
    """构建期专用：生成给 PyInstaller ``--icon`` 用的 .ico。

    这里**不加** bitmap_format，让 256 帧走 PNG 压缩，资源管理器读它没问题
    且体积小得多。托盘那条路才需要 BMP 帧。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico("ok", APP_ICO_SIZES, bitmap_format=None))
    log.info("已写出 %s", path)
