from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ICONSET = ROOT / "ClipFarmPilot.iconset"
ICONSET.mkdir(parents=True, exist_ok=True)
STATIC_ASSETS = ROOT.parent / "backend" / "app" / "static"
MOBILE_ASSETS = ROOT.parent / "mobile" / "assets"
MOBILE_ASSETS.mkdir(parents=True, exist_ok=True)


def render(size: int) -> Image.Image:
    scale = size / 1024
    image = Image.new("RGBA", (size, size), (6, 16, 26, 255))
    draw = ImageDraw.Draw(image)

    inset = round(72 * scale)
    radius = round(220 * scale)
    draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=radius,
        fill=(12, 34, 49, 255),
        outline=(43, 109, 137, 255),
        width=max(1, round(10 * scale)),
    )
    for radar_radius, alpha in ((312, 30), (224, 22)):
        radius_px = round(radar_radius * scale)
        center = size // 2
        draw.ellipse(
            (center - radius_px, center - radius_px, center + radius_px, center + radius_px),
            outline=(91, 214, 255, alpha),
            width=max(1, round(8 * scale)),
        )

    draw.polygon(
        [
            (round(512 * scale), round(164 * scale)),
            (round(610 * scale), round(420 * scale)),
            (round(842 * scale), round(510 * scale)),
            (round(842 * scale), round(570 * scale)),
            (round(612 * scale), round(532 * scale)),
            (round(654 * scale), round(806 * scale)),
            (round(512 * scale), round(728 * scale)),
            (round(370 * scale), round(806 * scale)),
            (round(412 * scale), round(532 * scale)),
            (round(182 * scale), round(570 * scale)),
            (round(182 * scale), round(510 * scale)),
            (round(414 * scale), round(420 * scale)),
        ],
        fill=(91, 214, 255, 255),
    )
    draw.polygon(
        [
            (round(472 * scale), round(423 * scale)),
            (round(638 * scale), round(512 * scale)),
            (round(472 * scale), round(601 * scale)),
        ],
        fill=(255, 189, 89, 255),
    )
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
