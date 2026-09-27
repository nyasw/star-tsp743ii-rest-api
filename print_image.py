#!/usr/bin/env python3
import argparse
import base64
import json
import urllib.request
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def prepare_image(path: Path, width: int) -> Image.Image:
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        height = round(source.height * width / source.width)
        source = source.resize((width, height), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(source)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        # Lighten dark saturated colors so thermal output does not become a
        # single black mass, then restore edge separation before dithering.
        gray = gray.point(lambda value: round(255 * (value / 255) ** 0.72))
        gray = ImageEnhance.Contrast(gray).enhance(1.08)
        gray = gray.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=3))
        return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def star_raster(image: Image.Image, left_margin_units: int) -> bytes:
    width, height = image.size
    if width % 8:
        raise ValueError("image width must be divisible by 8")
    row_bytes = width // 8
    if row_bytes > 255:
        raise ValueError("image is too wide")

    # Continuous raster page, normal quality, and a left margin measured in
    # 8-dot units. EOT mode 13 feeds to the cutter and performs one partial
    # cut when ESC * r B ends the raster job.
    output = bytearray(b"\x1b*rA\x1b*rP0\x00\x1b*rE13\x00")
    output.extend(b"\x1b*rml" + str(left_margin_units).encode("ascii") + b"\x00")
    output.extend(b"\x1b*rQ1\x00")

    pixels = image.load()
    for y in range(height):
        row = bytearray(row_bytes)
        for x in range(width):
            if pixels[x, y] == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        output.extend((0x62, row_bytes, 0))
        output.extend(row)

    output.extend(b"\x1b*rB")
    return bytes(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000/print/raw")
    parser.add_argument("--width", type=int, default=608)
    parser.add_argument("--left-margin", type=int, default=2)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    image = prepare_image(args.image, args.width)
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.preview)

    raster = star_raster(image, args.left_margin)
    if args.dry_run:
        print(json.dumps({"width": image.width, "height": image.height, "bytes": len(raster)}))
        return
    def send(data: bytes):
        body = json.dumps({"base64": base64.b64encode(data).decode("ascii")}).encode()
        request = urllib.request.Request(
            args.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode()

    print(send(raster))


if __name__ == "__main__":
    main()
