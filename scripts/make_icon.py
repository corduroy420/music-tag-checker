"""Draw the app icon (a music note with a check badge) and save it as .png, .icns and .ico."""
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build_assets")
SIZE = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded square background with a vertical gradient
    top, bottom = (124, 58, 237), (219, 39, 119)
    gradient = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        t = y / SIZE
        gd.line([(0, y), (SIZE, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([60, 60, SIZE - 60, SIZE - 60], radius=200, fill=255)
    img.paste(gradient, (0, 0), mask)
    # Beamed pair of eighth notes
    white = (255, 255, 255, 255)
    d.polygon([(400, 300), (760, 230), (760, 330), (400, 400)], fill=white)        # beam
    d.rectangle([400, 330, 440, 700], fill=white)                                   # left stem
    d.rectangle([720, 260, 760, 630], fill=white)                                   # right stem
    d.ellipse([290, 640, 445, 760], fill=white)                                     # left head
    d.ellipse([610, 570, 765, 690], fill=white)                                     # right head
    # Check badge
    d.ellipse([600, 640, 860, 900], fill=(16, 185, 129, 255), outline=white, width=18)
    d.line([(668, 772), (715, 820), (800, 720)], fill=white, width=34, joint="curve")
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    img = draw()
    img.save(os.path.join(OUT_DIR, "icon.png"))
    img.save(os.path.join(OUT_DIR, "icon.icns"))
    img.save(os.path.join(OUT_DIR, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Icons written to", OUT_DIR)


if __name__ == "__main__":
    main()
