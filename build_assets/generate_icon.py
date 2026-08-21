from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent
ICONSET = ROOT / "ClipFarmPilot.iconset"
ICONSET.mkdir(parents=True, exist_ok=True)
STATIC_ASSETS = ROOT.parent / "backend" / "app" / "static"
MOBILE_ASSETS = ROOT.parent / "mobile" / "assets"
MOBILE_ASSETS.mkdir(parents=True, exist_ok=True)


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], amount: float) -> tuple[int, int, int, int]:
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end)) + (255,)


def render(size: int) -> Image.Image:
    """Render one minimal, high-contrast pilot/play mark at any icon size."""
    scale = size / 1024
    image = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        draw.line((0, y, size, y), fill=_mix((10, 35, 52), (3, 12, 21), y / max(1, size - 1)))

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (-round(210 * scale), -round(260 * scale), round(870 * scale), round(720 * scale)),
        fill=(70, 214, 255, 48),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(max(1, round(155 * scale))))
    image.alpha_composite(glow)

    plane_points = [
        (round(512 * scale), round(166 * scale)),
        (round(600 * scale), round(414 * scale)),
        (round(824 * scale), round(500 * scale)),
        (round(824 * scale), round(562 * scale)),
        (round(610 * scale), round(532 * scale)),
        (round(650 * scale), round(810 * scale)),
        (round(512 * scale), round(730 * scale)),
        (round(374 * scale), round(810 * scale)),
        (round(414 * scale), round(532 * scale)),
        (round(200 * scale), round(562 * scale)),
        (round(200 * scale), round(500 * scale)),
        (round(424 * scale), round(414 * scale)),
    ]
    play_points = [
        (round(470 * scale), round(426 * scale)),
        (round(646 * scale), round(520 * scale)),
        (round(470 * scale), round(614 * scale)),
    ]

    shadow_mask = Image.new("L", (size, size), 0)
    shadow_draw = ImageDraw.Draw(shadow_mask)
    shadow_offset = round(20 * scale)
    shadow_draw.polygon([(x, y + shadow_offset) for x, y in plane_points], fill=190)
    shadow_draw.polygon([(x, y + shadow_offset) for x, y in play_points], fill=0)
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(max(1, round(28 * scale))))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow.putalpha(shadow_mask)
    image.alpha_composite(shadow)

    mark_mask = Image.new("L", (size, size), 0)
    mark_draw = ImageDraw.Draw(mark_mask)
    mark_draw.polygon(plane_points, fill=255)
    mark_draw.polygon(play_points, fill=0)
    mark = Image.new("RGBA", (size, size))
    mark_draw = ImageDraw.Draw(mark)
    for y in range(size):
        mark_draw.line((0, y, size, y), fill=_mix((133, 233, 255), (49, 190, 235), y / max(1, size - 1)))
    mark.putalpha(mark_mask)
    image.alpha_composite(mark)
    return image


targets = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

for filename, size in targets.items():
    render(size).save(ICONSET / filename, "PNG")

render(192).save(STATIC_ASSETS / "icon-192.png", "PNG")
render(512).save(STATIC_ASSETS / "icon-512.png", "PNG")
render(1024).save(MOBILE_ASSETS / "icon.png", "PNG")
render(1024).save(MOBILE_ASSETS / "adaptive-icon.png", "PNG")
render(256).save(ROOT / "ClipFarmPilot.ico", "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
