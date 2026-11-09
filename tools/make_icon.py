"""纯标准库生成 32x32 ICO 图标（不依赖 Pillow）。

图案：蓝紫渐变圆角底 + 三条会话横线，呼应「会话列表」。
"""
from __future__ import annotations

import struct
from pathlib import Path

W = H = 32
OUT = Path(__file__).resolve().parents[1] / "assets" / "icon.ico"


def rounded(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def build():
    # BGRA 像素，行序自上而下
    px = [[(0, 0, 0, 0) for _ in range(W)] for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if not rounded(x, y, 1, 1, 30, 30, 8):
                continue
            t = (x + y) / (2 * (W - 1))
            r = int(79 + (124 - 79) * t)
            g = int(140 + (92 - 140) * t)
            px[y][x] = (255, g, r, 255)

    # 三条白色会话线 + 左侧圆点
    lines = [(9, 23), (9, 20), (9, 17)]
    for i, (xa, xb) in enumerate(lines):
        y0 = 10 + i * 6
        for y in (y0, y0 + 1):
            for x in range(xa, xb + 1):
                px[y][x] = (255, 255, 255, 255)
        for y in (y0, y0 + 1):
            for x in (5, 6):
                px[y][x] = (255, 255, 255, 255)

    xor = bytearray()
    for y in range(H - 1, -1, -1):  # BMP 自下而上
        for x in range(W):
            xor += bytes(px[y][x])
    and_mask = bytes((W // 8) * H)  # 全 0：由 alpha 通道决定透明

    bih = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0,
                      len(xor) + len(and_mask), 0, 0, 0, 0)
    image = bih + bytes(xor) + and_mask
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(image), 22)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(icondir + entry + image)
    print(f"icon: {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
