"""Generate the app-icon assets from the source logo (``seventhslide.png``).

The source art is a landscape logo with transparent margins; application icons
must be *square*. This script trims the transparent border, centres the mark on
a square transparent canvas with a little breathing room, and writes the
per-platform icon files the build configs reference:

    seventhslide.ico        - Windows  (multi-resolution; electron-builder + the
                                         PyInstaller backend in lyrics.spec)
    seventhslide.icns        - macOS    (electron-builder app icon)
    seventhslide-icon.png   - 1024x1024 square master (Linux + runtime window icon)

Re-run after changing the logo (from the project root):

    python icons/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "seventhslide.png"
PADDING = 0.06  # fraction of the square left clear around the mark on each side


def build_square_master(size: int = 1024) -> Image.Image:
    img = Image.open(SOURCE).convert("RGBA")

    # Trim fully-transparent borders so the mark, not the source canvas, drives
    # the framing. Fall back to the whole image if it has no alpha channel.
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    cw, ch = img.size
    content = max(cw, ch)
    side = round(content / (1 - 2 * PADDING))

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - cw) // 2, (side - ch) // 2), img)
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    master = build_square_master(1024)

    png_path = ROOT / "seventhslide-icon.png"
    master.save(png_path)
    print(f"wrote {png_path.name} ({master.size[0]}x{master.size[1]})")

    ico_path = ROOT / "seventhslide.ico"
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    master.save(ico_path, format="ICO", sizes=sizes)
    print(f"wrote {ico_path.name} ({', '.join(str(s) for s, _ in sizes)})")

    icns_path = ROOT / "seventhslide.icns"
    try:
        master.save(icns_path, format="ICNS")
        print(f"wrote {icns_path.name}")
    except Exception as err:  # noqa: BLE001 - icns is best-effort off macOS
        print(f"skipped {icns_path.name}: {err} "
              "(electron-builder will convert the PNG on macOS)")


if __name__ == "__main__":
    main()
