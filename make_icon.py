#!/usr/bin/env python3
"""Generate noteXnote app icon — two mirrored notes, stems crossing at X."""
from PIL import Image, ImageDraw, ImageFilter
import math
import os
import subprocess

SIZE = 1024


def make_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, 'RGBA')
    s = size / 1024.0

    # Rounded-square background
    m = int(40 * s)
    cr = int(200 * s)
    draw.rounded_rectangle([m, m, size - m, size - m],
                           radius=cr, fill=(26, 26, 46, 255))

    # Subtle inner glow ring
    m2 = int(55 * s)
    cr2 = int(188 * s)
    draw.rounded_rectangle([m2, m2, size - m2, size - m2],
                           radius=cr2, fill=None,
                           outline=(50, 50, 80, 80), width=max(1, int(2 * s)))

    cx, cy = size // 2, size // 2
    stem_half = int(230 * s)
    stem_w = max(6, int(46 * s))
    head_r = int(78 * s)
    accent = (76, 175, 80)       # #4CAF50 — matches the app's waveform green
    accent_dark = (27, 94, 32)   # #1B5E20 — matches the app's waveform outline

    def rounded_bar(p1, p2, width, fill):
        """A thick line with round caps, drawn as a capsule polygon."""
        x1, y1 = p1
        x2, y2 = p2
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        nx, ny = -dy / length * width / 2, dx / length * width / 2
        poly = [
            (x1 + nx, y1 + ny), (x2 + nx, y2 + ny),
            (x2 - nx, y2 - ny), (x1 - nx, y1 - ny),
        ]
        draw.polygon(poly, fill=fill)
        r = width / 2
        draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=fill)
        draw.ellipse([x2 - r, y2 - r, x2 + r, y2 + r], fill=fill)

    # Two note stems crossing at the icon's center, forming the X. Each
    # stem runs from a low outer corner up to a high outer corner on the
    # opposite side — a mirror image of the other — with a notehead
    # (filled circle) at its lower end, reading as two eighth notes
    # crossing.
    for mirror in (False, True):
        sign = -1 if mirror else 1
        bottom = (cx - sign * stem_half, cy + stem_half)
        top = (cx + sign * stem_half, cy - stem_half)
        # Shadow
        rounded_bar((bottom[0] + int(6 * s), bottom[1] + int(8 * s)),
                    (top[0] + int(6 * s), top[1] + int(8 * s)),
                    stem_w + int(6 * s), (0, 0, 0, 50))
        # Outline
        rounded_bar(bottom, top, stem_w + int(6 * s), accent_dark + (255,))
        # Fill
        rounded_bar(bottom, top, stem_w, accent + (255,))
        # Notehead at the low end
        hx, hy = bottom
        draw.ellipse([hx - head_r, hy - head_r, hx + head_r, hy + head_r],
                     fill=accent_dark + (255,))
        inner = head_r - int(10 * s)
        draw.ellipse([hx - inner, hy - inner, hx + inner, hy + inner],
                     fill=accent + (255,))

    # Soft highlight glow over the crossing point for a bit of shine
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow, 'RGBA')
    gr = int(120 * s)
    gdraw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=(255, 255, 255, 40))
    glow = glow.filter(ImageFilter.GaussianBlur(max(1, int(30 * s))))
    img = Image.alpha_composite(img, glow)

    return img


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    iconset = os.path.join(script_dir, 'noteXnote.iconset')
    os.makedirs(iconset, exist_ok=True)

    print("Generating icon at 1024x1024...")
    master = make_icon(1024)

    sizes = [16, 32, 128, 256, 512]
    for sz in sizes:
        icon = master.resize((sz, sz), Image.LANCZOS)
        icon.save(os.path.join(iconset, f'icon_{sz}x{sz}.png'))
        sz2 = sz * 2
        if sz2 <= 1024:
            icon2 = master.resize((sz2, sz2), Image.LANCZOS)
            icon2.save(os.path.join(iconset, f'icon_{sz}x{sz}@2x.png'))
    master.save(os.path.join(iconset, 'icon_512x512@2x.png'))
    print(f"  Saved {len(os.listdir(iconset))} PNGs to {iconset}")

    icns_path = os.path.join(script_dir, 'noteXnote.icns')
    subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', icns_path], check=True)
    print(f"  Created {icns_path}")

    preview = os.path.join(script_dir, 'icon_preview.png')
    master.save(preview)
    print(f"  Preview: {preview}")


if __name__ == '__main__':
    main()
