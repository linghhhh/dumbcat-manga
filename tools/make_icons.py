"""从一张方图生成安卓图标（含自适应图标与启动页 logo）。

源图：tools/icon_src.jpg —— **不随仓库发布**（.gitignore 里），需要重新生成图标时
自己准备一张 810x1080 或任意尺寸、主体居中的呆猫头照片放回该路径；换尺寸会自动
等比缩放裁剪框。仓库里的 mipmap 图标是已经生成好的成品，不改图标就不用跑这个脚本。

输出：
  app/src/main/res/mipmap-*/ic_launcher.png, ic_launcher_round.png
  app/src/main/res/mipmap-*/ic_launcher_foreground.png
  app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml, ic_launcher_round.xml
  app/src/main/assets/appdata/web/assets/logo.png（网页/启动页用）
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "icon_src.jpg"
RES = ROOT / "app" / "src" / "main" / "res"
WEB_ASSETS = ROOT / "app" / "src" / "main" / "assets" / "appdata" / "web" / "assets"

BG = (253, 243, 227)          # 奶油底，和 App 主题一致
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}
ADAPTIVE = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}

# 照片里猫头的大致位置（810x1080 原图）
CROP_BOX = (128, 158, 772, 802)   # 644x644，猫头充满圆


def head_image() -> Image.Image:
    img = Image.open(SRC).convert("RGB")
    box = CROP_BOX
    if img.size != (810, 1080):
        # 尺寸变了就按比例缩放裁剪框
        sx = img.width / 810
        sy = img.height / 1080
        box = (int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy))
    head = img.crop(box).resize((900, 900), Image.LANCZOS)
    # 轻微提亮 + 锐化，像素图缩小后更精神
    head = head.point(lambda v: min(255, int(v * 1.06 + 6)))
    return head.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))


def circle_mask(size: int, shrink: float = 0.0) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = size * shrink
    draw.ellipse((inset, inset, size - inset, size - inset), fill=255)
    return mask


def compose(size: int, head: Image.Image, circle_ratio: float) -> Image.Image:
    """奶油底 + 圆形猫头，返回 size×size 的图。"""
    canvas = Image.new("RGBA", (size, size), BG + (255,))
    diameter = int(size * circle_ratio)
    inner = head.resize((diameter, diameter), Image.LANCZOS)
    mask = circle_mask(diameter, 0.0)
    # 圆形边缘稍微羽化，贴上去不会有锯齿
    mask = mask.filter(ImageFilter.GaussianBlur(max(0.6, size / 140)))
    offset = ((size - diameter) // 2, (size - diameter) // 2)

    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        (offset[0] - size * 0.015, offset[1] - size * 0.015,
         offset[0] + diameter + size * 0.015, offset[1] + diameter + size * 0.015),
        fill=(47, 107, 255, 38),
    )
    canvas = Image.alpha_composite(canvas, ring)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer.paste(inner, offset, mask)
    return Image.alpha_composite(canvas, layer)


def foreground(size: int, head: Image.Image) -> Image.Image:
    """自适应图标前景：透明底，圆头放在安全区里。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    diameter = int(size * 0.60)
    inner = head.resize((diameter, diameter), Image.LANCZOS)
    mask = circle_mask(diameter).filter(ImageFilter.GaussianBlur(max(0.6, size / 140)))
    canvas.paste(inner, ((size - diameter) // 2, (size - diameter) // 2), mask)
    return canvas


def main() -> None:
    head = head_image()

    for density, size in DENSITIES.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        icon = compose(size, head, 0.92)
        icon.save(folder / "ic_launcher.png")
        icon.save(folder / "ic_launcher_round.png")
        print("wrote", folder / "ic_launcher.png", size)

    for density, size in ADAPTIVE.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        foreground(size, head).save(folder / "ic_launcher_foreground.png")
        print("wrote foreground", density, size)

    anydpi = RES / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    xml = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/icon_bg" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
"""
    (anydpi / "ic_launcher.xml").write_text(xml, encoding="utf-8")
    (anydpi / "ic_launcher_round.xml").write_text(xml, encoding="utf-8")

    colors = RES / "values" / "colors.xml"
    text = colors.read_text(encoding="utf-8")
    if "icon_bg" not in text:
        text = text.replace("</resources>", "    <color name=\"icon_bg\">#FDF3E3</color>\n</resources>")
        colors.write_text(text, encoding="utf-8")

    # 网页/启动页 logo
    WEB_ASSETS.mkdir(parents=True, exist_ok=True)
    compose(512, head, 0.92).save(WEB_ASSETS / "logo.png")
    print("wrote", WEB_ASSETS / "logo.png")

    # 预览图方便肉眼检查
    preview = ROOT / "tmp" / "icon_preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    compose(384, head, 0.92).save(preview)
    print("preview", preview)


if __name__ == "__main__":
    main()
