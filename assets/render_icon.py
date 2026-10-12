#!/usr/bin/env python3
"""Rasterize the original rect/line SVG project icon; requires Pillow for builds only."""
from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SCALE = 4
canvas = Image.new("RGBA", (1024 * SCALE, 1024 * SCALE), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)
for element in ET.parse(ROOT / "app-icon.svg").getroot():
    tag = element.tag.rsplit("}", 1)[-1]
    a = element.attrib
    def scaled(name, default=0):
        return round(float(a.get(name, default)) * SCALE)
    if tag == "rect":
        x, y, w, h = (scaled(name) for name in ["x", "y", "width", "height"])
        draw.rounded_rectangle([x, y, x + w, y + h], radius=scaled("rx"), fill=a["fill"])
    elif tag == "line":
        draw.line([scaled("x1"), scaled("y1"), scaled("x2"), scaled("y2")], fill=a["stroke"], width=scaled("stroke-width"))
    else:
        raise ValueError("Unsupported original icon shape: " + tag)
canvas.resize((1024, 1024), Image.Resampling.LANCZOS).save(ROOT / "app-icon.png")
