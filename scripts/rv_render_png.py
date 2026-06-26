#!/usr/bin/env python3
"""Render a captured-text evidence file as a PNG screenshot.

Usage:
    python3 scripts/rv_render_png.py \\
        --input .iar/evidence/rv-1-logs-tail.txt \\
        --output .iar/evidence/rv-1-logs-tail.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FONT_PATH_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/Library/Fonts/Courier New.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_PATH_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _measure(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    if not text:
        return 0, 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def render(input_path: Path, output_path: Path, *, font_size: int) -> None:
    raw_text = input_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines() or [""]

    font = _load_font(font_size)
    # Use a scratch draw to measure line widths.
    scratch = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(scratch)
    max_width = 0
    line_height = 0
    for line in lines:
        line_width, line_height = _measure(draw, line, font)
        if line_width > max_width:
            max_width = line_width

    margin = 24
    line_spacing = int(line_height * 1.2)
    width = min(max_width + 2 * margin, 4096)
    height = line_spacing * len(lines) + 2 * margin

    image = Image.new("RGB", (width, height), (24, 24, 30))
    draw = ImageDraw.Draw(image)

    for index, line in enumerate(lines):
        y = margin + index * line_spacing
        draw.text((margin, y), line, fill=(220, 220, 230), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    print(f"[rv-render] {input_path} → {output_path} ({width}x{height})", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font-size", type=int, default=18)
    args = parser.parse_args(argv)
    render(args.input, args.output, font_size=args.font_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))