#!/usr/bin/env python3
import base64
import hmac
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError


DEVICE = os.environ.get("PRINTER_DEVICE", "/dev/usb/lp0")
TOKEN = os.environ.get("PRINTER_API_TOKEN", "")
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", "8388608"))
DEFAULT_LEFT_MARGIN = int(os.environ.get("LEFT_MARGIN_CHARS", "1"))
OPENAPI_PATH = os.environ.get("OPENAPI_PATH", "/opt/star-printer-api/openapi.json")
WEB_UI_PATH = os.environ.get("WEB_UI_PATH", "/opt/star-printer-api/index.html")
LOCK = threading.Lock()
Image.MAX_IMAGE_PIXELS = 30_000_000


def device_ready():
    return os.path.exists(DEVICE) and os.access(DEVICE, os.W_OK)


def write_printer(data: bytes):
    if not device_ready():
        raise FileNotFoundError(f"printer is not writable: {DEVICE}")
    with LOCK, open(DEVICE, "wb", buffering=0) as printer:
        remaining = memoryview(data)
        while remaining:
            try:
                written = printer.write(remaining)
            except InterruptedError:
                continue
            if not written:
                raise OSError("printer stopped accepting data during write")
            remaining = remaining[written:]


class Handler(BaseHTTPRequestHandler):
    server_version = "StarPrinterAPI/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status, payload):
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        with open(path, "rb") as source:
            body = source.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not TOKEN:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {TOKEN}")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY:
            raise ValueError(f"body must be 1..{MAX_BODY} bytes")
        return self.rfile.read(length)

    def read_json(self):
        try:
            return json.loads(self.read_body())
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc

    def require_auth(self):
        if self.authorized():
            return True
        self.send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            try:
                self.send_file(WEB_UI_PATH, "text/html; charset=utf-8")
            except OSError as exc:
                self.send_json(503, {"ok": False, "error": f"Web UI unavailable: {exc}"})
            return
        if path == "/health":
            ready = device_ready()
            self.send_json(200 if ready else 503, {
                "ok": ready,
                "printer": "Star Micronics TSP743II",
                "device": DEVICE,
                "writable": ready,
            })
            return
        if path == "/openapi.json":
            try:
                with open(OPENAPI_PATH, "r", encoding="utf-8") as spec:
                    self.send_json(200, json.load(spec))
            except (OSError, json.JSONDecodeError) as exc:
                self.send_json(503, {"ok": False, "error": f"OpenAPI unavailable: {exc}"})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.require_auth():
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path in ("/print/image", "/image"):
                self.print_image(parse_qs(parsed.query))
                return
            payload = self.read_json()
            if parsed.path in ("/print/text", "/print"):
                self.print_text(payload)
            elif parsed.path in ("/print/raw", "/raw"):
                self.print_raw(payload)
            else:
                self.send_json(404, {"ok": False, "error": "not found"})
        except (ValueError, TypeError, KeyError, UnidentifiedImageError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except (OSError, UnicodeError) as exc:
            self.send_json(503, {"ok": False, "error": str(exc)})

    def print_text(self, payload):
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        encoding = payload.get("encoding", "cp932")
        feed = payload.get("feed", 3)
        cut = payload.get("cut", "partial")
        left_margin = payload.get("left_margin", DEFAULT_LEFT_MARGIN)
        if not isinstance(feed, int) or not 0 <= feed <= 20:
            raise ValueError("feed must be an integer from 0 to 20")
        if not isinstance(left_margin, int) or not 0 <= left_margin <= 20:
            raise ValueError("left_margin must be an integer from 0 to 20")
        if cut not in ("none", "full", "partial"):
            raise ValueError("cut must be none, full, or partial")
        encoded = text.encode(encoding)
        # ESC l n sets the Star Line Mode left margin in character-pitch units.
        data = b"\x1b@\x1bl" + bytes((left_margin,)) + encoded
        if not data.endswith(b"\n"):
            data += b"\n"
        data += b"\n" * feed
        if cut == "full":
            data += b"\x1bd\x02"
        elif cut == "partial":
            data += b"\x1bd\x03"
        write_printer(data)
        self.send_json(200, {
            "ok": True,
            "bytes": len(data),
            "cut": cut,
            "left_margin": left_margin,
        })

    def print_raw(self, payload):
        raw = payload.get("base64")
        if not isinstance(raw, str) or not raw:
            raise ValueError("base64 must be a non-empty string")
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError("invalid base64") from exc
        if not data or len(data) > MAX_BODY:
            raise ValueError(f"decoded data must be 1..{MAX_BODY} bytes")
        write_printer(data)
        self.send_json(200, {"ok": True, "bytes": len(data)})

    def print_image(self, query):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        body = self.read_body()
        options = {}
        if content_type == "application/json":
            try:
                options = json.loads(body)
                encoded = options.get("base64") or options.get("image_base64")
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("base64 must be a non-empty string")
                image_data = base64.b64decode(encoded, validate=True)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON") from exc
            except Exception as exc:
                if isinstance(exc, ValueError):
                    raise
                raise ValueError("invalid image base64") from exc
        elif content_type.startswith("image/"):
            image_data = body
        else:
            raise ValueError("Content-Type must be image/jpeg, image/png, image/webp, or application/json")

        def option(name, default):
            if name in query and query[name]:
                return query[name][0]
            return options.get(name, default)

        try:
            width = int(option("width", 608))
            left_margin = int(option("left_margin", 2))
        except (TypeError, ValueError) as exc:
            raise ValueError("width and left_margin must be integers") from exc
        cut = option("cut", "partial")
        dither = option("dither", "floyd-steinberg")
        if width < 8 or width > 640 or width % 8:
            raise ValueError("width must be a multiple of 8 from 8 to 640")
        if not 0 <= left_margin <= 20:
            raise ValueError("left_margin must be an integer from 0 to 20")
        if width + left_margin * 8 > 640:
            raise ValueError("width plus left margin exceeds 640 dots")
        if cut not in ("none", "full", "partial"):
            raise ValueError("cut must be none, full, or partial")
        if dither not in ("floyd-steinberg", "threshold"):
            raise ValueError("dither must be floyd-steinberg or threshold")

        try:
            with Image.open(io.BytesIO(image_data)) as source:
                source = ImageOps.exif_transpose(source)
                source.load()
                if source.width <= 0 or source.height <= 0:
                    raise ValueError("invalid image dimensions")
                height = round(source.height * width / source.width)
                if height <= 0 or height > 4000:
                    raise ValueError("resized image height must be 1..4000 dots")
                if source.mode in ("RGBA", "LA") or "transparency" in source.info:
                    rgba = source.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    source = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    source = source.convert("RGB")
                source = source.resize((width, height), Image.Resampling.LANCZOS)
        except Image.DecompressionBombError as exc:
            raise ValueError("image is too large") from exc

        gray = ImageOps.autocontrast(ImageOps.grayscale(source), cutoff=1)
        gray = gray.point(lambda value: round(255 * (value / 255) ** 0.72))
        gray = ImageEnhance.Contrast(gray).enhance(1.08)
        gray = gray.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=3))
        mode = Image.Dither.FLOYDSTEINBERG if dither == "floyd-steinberg" else Image.Dither.NONE
        monochrome = gray.convert("1", dither=mode)

        eot_mode = {"none": "1", "full": "9", "partial": "13"}[cut]
        raster = bytearray(b"\x1b*rA\x1b*rP0\x00")
        raster.extend(b"\x1b*rE" + eot_mode.encode("ascii") + b"\x00")
        raster.extend(b"\x1b*rml" + str(left_margin).encode("ascii") + b"\x00")
        raster.extend(b"\x1b*rQ1\x00")
        packed = monochrome.tobytes()
        row_bytes = width // 8
        for y in range(monochrome.height):
            row = packed[y * row_bytes:(y + 1) * row_bytes]
            raster.extend((0x62, row_bytes, 0))
            raster.extend(byte ^ 0xFF for byte in row)
        raster.extend(b"\x1b*rB")

        write_printer(bytes(raster))
        self.send_json(200, {
            "ok": True,
            "bytes": len(raster),
            "width": monochrome.width,
            "height": monochrome.height,
            "left_margin": left_margin,
            "dither": dither,
            "cut": cut,
        })


if __name__ == "__main__":
    host = os.environ.get("LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("LISTEN_PORT", "8000"))
    print(f"Listening on http://{host}:{port}; printer={DEVICE}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
