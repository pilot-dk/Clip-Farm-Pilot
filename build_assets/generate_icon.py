from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ICONSET = ROOT / "ClipFarmPilot.iconset"
ICONSET.mkdir(parents=True, exist_ok=True)


def render(size: int) -> Image.Image:
    scale = size / 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    inset = round(72 * scale)
    radius = round(220 * scale)
    draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=radius,
        fill=(185, 243, 74, 255),
    )
    draw.rounded_rectangle(
        (round(226 * scale), round(248 * scale), round(280 * scale), round(776 * scale)),
        radius=round(27 * scale),
        fill=(17, 21, 8, 255),
    )
    draw.polygon(
        [
            (round(390 * scale), round(304 * scale)),
            (round(770 * scale), round(512 * scale)),
            (round(390 * scale), round(720 * scale)),
        ],
        fill=(17, 21, 8, 255),
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
