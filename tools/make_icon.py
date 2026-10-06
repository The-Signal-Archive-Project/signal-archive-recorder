# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Draw the app icon: a waveform rising out of a noise floor, on a dark blue tile.

Writes the PNG the app uses (package data) and the multi-size .ico the Windows
installer uses. Run again after changing the design:

    QT_QPA_PLATFORM=offscreen python tools/make_icon.py
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parent.parent
PNG = ROOT / "src" / "signal_archive_recorder" / "data" / "icon.png"
ICO = ROOT / "installer" / "windows" / "icon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#173a6b"))
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 48 * s, 48 * s)

    # The noise floor: a faint jagged line (dropped at small sizes, where it's mush).
    if size >= 48:
        noise = QPainterPath()
        for i in range(0, 201, 4):
            x = (28 + i) * s
            y = (172 + 7 * math.sin(i * 1.7) * math.cos(i * 0.37)) * s
            noise.lineTo(QPointF(x, y)) if i else noise.moveTo(QPointF(x, y))
        p.setPen(QPen(QColor(120, 160, 210, 150), max(1.0, 5 * s)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(noise)

    # The signal: a burst of sine rising out of it.
    wave = QPainterPath()
    for i in range(0, 201):
        x = (28 + i) * s
        envelope = math.exp(-(((i - 100) / 46) ** 2))
        y = (128 - 70 * envelope * math.sin(i * 0.22)) * s
        wave.lineTo(QPointF(x, y)) if i else wave.moveTo(QPointF(x, y))
    p.setPen(
        QPen(
            QColor("#ffffff"),
            max(1.5, 14 * s),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(wave)

    # A recording dot.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2e9d4a"))
    p.drawEllipse(QRectF(186 * s, 30 * s, 40 * s, 40 * s))
    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")  # type: ignore[call-overload]  # the stubs want bytes; runtime wants str
    return bytes(data.data())


def write_ico(path: Path, images: list[QImage]) -> None:
    """An .ico of PNG-compressed entries (supported since Windows Vista)."""
    blobs = [png_bytes(i) for i in images]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    for image, blob in zip(images, blobs, strict=True):
        w = image.width() if image.width() < 256 else 0  # 0 means 256
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + b"".join(blobs))


def main() -> int:
    app = QGuiApplication(sys.argv[:1])
    PNG.write_bytes(png_bytes(draw(256)))
    write_ico(ICO, [draw(size) for size in SIZES])
    print(f"wrote {PNG.relative_to(ROOT)} and {ICO.relative_to(ROOT)}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
