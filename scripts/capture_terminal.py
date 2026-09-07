"""Render an actual script(1) ANSI capture as a terminal screen PNG (requires pyte/Pillow)."""

import argparse
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont


def capture(source, output, columns=100, rows=36):
    screen = pyte.Screen(columns, rows)
    stream = pyte.Stream(screen)
    data = source.read_bytes().decode("utf-8", errors="replace")
    # script(1)'s metadata header is not terminal output.
    if data.startswith("Script started on"):
        data = data.split("\n", 1)[1]
    stream.feed(data)
    normal = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
    bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 18)
    cell, height, margin = 11, 25, 20
    image = Image.new("RGB", (columns * cell + margin * 2, rows * height + margin * 2), "#111827")
    draw = ImageDraw.Draw(image)
    colours = {
        "default": "#e5e7eb",
        "black": "#111827",
        "red": "#f87171",
        "green": "#86efac",
        "yellow": "#facc15",
        "blue": "#60a5fa",
        "magenta": "#e879f9",
        "cyan": "#67e8f9",
        "white": "#e5e7eb",
        "brown": "#facc15",
    }
    for row in range(rows):
        for col in range(columns):
            char = screen.buffer[row][col]
            colour = colours.get(char.fg, "#" + char.fg if len(char.fg) == 6 else "#e5e7eb")
            draw.text(
                (margin + col * cell, margin + row * height),
                char.data,
                font=bold if char.bold else normal,
                fill=colour,
            )
    image.save(output)
    output.with_suffix(".txt").write_text("\n".join(screen.display) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    capture(args.source, args.output)
