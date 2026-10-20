"""Turn assets/logo.png into the app icons (build_assets/icon.png, .icns, .ico).

The logo is trimmed, centred on a white rounded square and scaled up. Small sizes get slightly
thicker lines so the hand-drawn strokes stay visible in the Dock, taskbar and title bar.
"""
import os

from PIL import Image, ImageChops, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(ROOT, "assets", "logo.png")
OUT_DIR = os.path.join(ROOT, "build_assets")
SIZE = 1024
MARGIN = 60          # transparent edge around the rounded square (macOS icon grid)
PADDING = 150        # space between the square's edge and the drawing
SMALL_SIZES = [16, 32, 48, 64, 128, 256]


def trimmed_logo() -> Image.Image:
    """The logo cropped to its drawing, with white turned transparent."""
    logo = Image.open(LOGO).convert("RGBA")
    white = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    box = ImageChops.difference(logo, white).convert("L").point(lambda p: 255 if p > 24 else 0).getbbox()
    logo = logo.crop(box) if box else logo
    # Alpha = how far each pixel is from white, so strokes keep soft edges on any background.
    alpha = ImageChops.invert(logo.convert("L"))
    for channel in logo.split()[:3]:  # coloured strokes (the green check) count as ink too
        alpha = ImageChops.lighter(alpha, ImageChops.invert(channel).point(lambda p: min(255, p * 2)))
    logo.putalpha(alpha)
    return logo


def compose(logo: Image.Image, thicken: int = 0) -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN], radius=200, fill=255)
    canvas.paste(Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255)), (0, 0), mask)
    ImageDraw.Draw(canvas).rounded_rectangle([MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN], radius=200,
                                             outline=(0, 0, 0, 40), width=4)  # faint edge on white backgrounds
    room = SIZE - 2 * (MARGIN + PADDING)
    scale = room / max(logo.size)
    art = logo.resize((round(logo.width * scale), round(logo.height * scale)), Image.LANCZOS)
    if thicken:  # stamp the drawing in a small circle of offsets: thicker strokes, same colours
        r = thicken
        bold = Image.new("RGBA", (art.width + 2 * r, art.height + 2 * r), (0, 0, 0, 0))
        for dx in range(-r, r + 1, 2):
            for dy in range(-r, r + 1, 2):
                if dx * dx + dy * dy <= r * r:
                    bold.alpha_composite(art, (r + dx, r + dy))
        art = bold
    canvas.alpha_composite(art, ((SIZE - art.width) // 2, (SIZE - art.height) // 2))
    return canvas


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    logo = trimmed_logo()
    full = compose(logo)
    bold = compose(logo, thicken=9)  # used for the small sizes only
    small = [(bold if s <= 64 else full).resize((s, s), Image.LANCZOS) for s in SMALL_SIZES]
    full.save(os.path.join(OUT_DIR, "icon.png"))
    full.save(os.path.join(OUT_DIR, "icon.icns"), append_images=small)
    small[-1].save(os.path.join(OUT_DIR, "icon.ico"), sizes=[(s, s) for s in SMALL_SIZES], append_images=small[:-1])
    for s in (32, 128):
        (bold if s <= 64 else full).resize((s, s), Image.LANCZOS).save(os.path.join(OUT_DIR, f"preview-{s}.png"))
    print("Icons written to", OUT_DIR)


if __name__ == "__main__":
    main()
