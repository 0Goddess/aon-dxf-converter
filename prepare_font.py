#!/usr/bin/env python3
"""Merge Fontsource's Noto Sans TC webfont slices into one local TrueType font."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from fontTools.merge import Merger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fontsource-dir",
        type=Path,
        default=Path("node_modules/@fontsource/noto-sans-tc"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    args = parser.parse_args()

    files = sorted(args.fontsource_dir.glob("files/*-400-normal.woff"))
    if len(files) < 100:
        raise RuntimeError(f"Noto Sans TC 字型切片不完整：只找到 {len(files)} 個檔案。")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "NotoSansTC-Regular.ttf"
    font = Merger().merge([str(path) for path in files])
    font.flavor = None
    font.save(destination)

    license_file = args.fontsource_dir / "LICENSE"
    if license_file.is_file():
        shutil.copy2(license_file, args.output_dir / "OFL-1.1.txt")
    print(f"font={destination} bytes={destination.stat().st_size} slices={len(files)}")


if __name__ == "__main__":
    main()


