# Star TSP743II REST API

A small, dependency-light REST API and Web UI for a USB-connected Star
Micronics TSP743II receipt printer on Linux. It writes Star Line Mode commands
directly to `/dev/usb/lp0`, without CUPS.

The service can accept color JPEG, PNG, or WebP images, resize and enhance them,
apply Floyd–Steinberg dithering, and print them as monochrome raster graphics.
Raster jobs use the printer's EOT mode 13 so the printer feeds to the cutter and
performs one partial cut at the correct time.

## Features

- Responsive Web UI at `/`
- CP932 Japanese text printing
- Server-side color image conversion and dithering
- Configurable print width, margin, cut mode, and dithering method
- Raw Star command passthrough
- OpenAPI 3.1 document at `/openapi.json`
- USB short-write handling
- Serialized access to one printer
- Hardened systemd service
- Optional Bearer token authentication

## Requirements

- Linux with Python 3
- Star TSP743II connected over USB and exposed as `/dev/usb/lp0`
- Pillow 12.x (`python3-pil` on Ubuntu)
- A user or service account with access to the `lp` group

The default image layout is tuned for 80 mm paper:

- 608-dot image width
- 16-dot left margin
- Approximately 2 mm safety margin on each side

## Install on Ubuntu

Install Pillow and create a restricted service account:

```bash
sudo apt-get install -y python3-pil
sudo useradd --system --no-create-home --shell /usr/sbin/nologin star-printer-api
sudo install -d -o root -g root -m 0755 /opt/star-printer-api
```

Copy the application files:

```bash
sudo install -o root -g root -m 0755 app.py /opt/star-printer-api/app.py
sudo install -o root -g root -m 0644 index.html /opt/star-printer-api/index.html
sudo install -o root -g root -m 0644 openapi.json /opt/star-printer-api/openapi.json
sudo install -o root -g root -m 0644 star-printer-api.service /etc/systemd/system/star-printer-api.service
```

Create `/etc/star-printer-api.env`:

```ini
PRINTER_DEVICE=/dev/usb/lp0
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8000
LEFT_MARGIN_CHARS=1
```

For access over Tailscale, set `LISTEN_HOST` to the machine's Tailscale IP. Do
not bind to an untrusted network without configuring `PRINTER_API_TOKEN` or
another authentication layer.

Enable the service:

```bash
sudo chmod 0600 /etc/star-printer-api.env
sudo systemctl daemon-reload
sudo systemctl enable --now star-printer-api.service
```

Then open `http://HOST:8000/`.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Printer device health |
| `GET` | `/openapi.json` | OpenAPI 3.1 specification |
| `POST` | `/print/text` | Print CP932 text |
| `POST` | `/print/image` | Convert and print a color image |
| `POST` | `/print/raw` | Send Base64-encoded Star commands |

Legacy aliases `/print`, `/image`, and `/raw` remain available.

### Print text

```bash
curl -X POST http://HOST:8000/print/text \
  -H 'Content-Type: application/json' \
  -d '{"text":"テスト印刷","feed":3,"cut":"partial","left_margin":1}'
```

### Print a color image

```bash
curl --data-binary @photo.jpg \
  -H 'Content-Type: image/jpeg' \
  'http://HOST:8000/print/image'
```

Optional query parameters:

- `width`: output width in dots, divisible by 8; default `608`
- `left_margin`: left margin in 8-dot units; default `2`
- `dither`: `floyd-steinberg` or `threshold`
- `cut`: `partial`, `full`, or `none`

The endpoint also accepts `application/json` with the image in a `base64`
property. Query parameters override JSON options.

### Print raw commands

```bash
curl -X POST http://HOST:8000/print/raw \
  -H 'Content-Type: application/json' \
  -d '{"base64":"G0BIZWxsbwo="}'
```

## Authentication

Authentication is disabled when `PRINTER_API_TOKEN` is unset. To enable it,
add a random token to `/etc/star-printer-api.env`:

```ini
PRINTER_API_TOKEN=replace-with-a-long-random-value
```

Then send `Authorization: Bearer TOKEN` with POST requests. The health check,
Web UI, and OpenAPI document remain readable without a token. The current Web
UI does not prompt for a token, so place an authenticated deployment behind a
trusted reverse proxy or use API clients directly.

## Standalone image client

`print_image.py` performs the same basic raster preparation on the client and
sends Star command bytes to `/print/raw`:

```bash
python3 print_image.py photo.jpg \
  --url http://HOST:8000/print/raw \
  --preview preview.png
```

## Notes

- This project targets Star Line Mode and the TSP743II. Other printers may use
  different commands, print widths, or cutter behavior.
- A successful response means the bytes were accepted by the Linux USB device;
  it does not guarantee that paper, cover, or cutter hardware is healthy.
- The API is intended for one locally attached printer.

This project is not affiliated with or endorsed by Star Micronics.
