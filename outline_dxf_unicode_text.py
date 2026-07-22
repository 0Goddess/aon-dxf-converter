#!/usr/bin/env python3
"""Replace every DXF TEXT entity containing non-ASCII text with vector outlines."""

from __future__ import annotations

import argparse
from pathlib import Path

import ezdxf
from ezdxf import path
from ezdxf.addons import text2path
from ezdxf.fonts import fonts


PUNCTUATION_MAP = str.maketrans(
    {
        "／": "/",
        "：": ":",
        "；": ";",
        "｜": "|",
    }
)


def convert(source: Path, destination: Path, font_file: Path) -> tuple[int, int]:
    fonts.font_manager.scan_folder(font_file.parent)
    document = ezdxf.readfile(source)

    outline_style = "AON_TC"
    if outline_style not in document.styles:
        document.styles.add(outline_style, font=font_file.name)
    document.styles.get(outline_style).dxf.font = font_file.name

    modelspace = document.modelspace()
    converted_texts = 0
    outline_polylines = 0

    for entity in list(modelspace.query("TEXT")):
        content = entity.dxf.text
        if not any(ord(character) >= 128 for character in content):
            continue

        entity.dxf.style = outline_style
        entity.dxf.text = content.translate(PUNCTUATION_MAP)
        glyph_paths = text2path.make_paths_from_entity(entity)
        attributes = {
            "layer": entity.dxf.layer,
            "color": entity.dxf.color,
            "linetype": entity.dxf.linetype,
        }
        tolerance = max(entity.dxf.height / 30.0, 0.05)
        for polyline in path.to_lwpolylines(
            glyph_paths,
            distance=tolerance,
            segments=4,
            dxfattribs=attributes,
        ):
            modelspace.add_entity(polyline)
            outline_polylines += 1

        modelspace.delete_entity(entity)
        converted_texts += 1

    # Remaining text is ASCII only; use AutoCAD's built-in SHX font.
    document.styles.get(outline_style).dxf.font = "txt.shx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.saveas(destination, encoding="utf-8", fmt="asc")
    return converted_texts, outline_polylines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("font_file", type=Path)
    args = parser.parse_args()

    converted, polylines = convert(args.source, args.destination, args.font_file)
    print(f"converted_texts={converted}")
    print(f"outline_polylines={polylines}")


if __name__ == "__main__":
    main()

