#!/usr/bin/env python3
"""Compile the unchanged ESP32 C renderer with GCC and export lossless PNG atlases.

Run on Linux/WSL: python3 tools/avatar-build.py
No Pillow, browser, network, or ESP-IDF dependency is required.
"""
from pathlib import Path
import hashlib
import json
import math
import struct
import subprocess
import tempfile
import zlib

UI = Path(__file__).resolve().parents[1]
OUT = UI / 'public' / 'esp32-avatar'
SOURCE = OUT / 'source'
COMMIT = 'b139b45064b4dcecf7bfe97e75bc7f99c10c28b6'
FPS, FRAMES, COLUMNS = 20, 160, 16


def png(width, height, pixels):
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    rows = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def main():
    sequences = [('boot', 0, 0, False), ('idle', 1, 0, False), ('thinking', 3, 0, False),
                 ('error', 5, 0, False), ('off', 6, 0, False), ('happy', 1, 0, True)]
    for name, mode in [('listening', 2), ('speaking', 4)]:
        sequences += [(f'{name}-{i}', mode, i / 4, False) for i in range(5)]
    manifest = dict(sourceCommit=COMMIT, width=64, height=64, tileWidth=128, columns=COLUMNS,
                    fps=FPS, format='RGB888 from original RGB565; second 64px tile is original dim palette',
                    sequences={}, sourceSha256={})
    for path in SOURCE.iterdir():
        if path.is_file():
            manifest['sourceSha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='muse-avatar-') as temp:
        exe = Path(temp) / 'avatar-export'
        subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-I', str(SOURCE),
                        str(UI / 'tools' / 'avatar-export.c'), str(SOURCE / 'muse_pixel.c'),
                        '-lm', '-o', str(exe)], check=True)
        for name, mode, level, pet in sequences:
            count = 40 if pet else FRAMES
            raw = subprocess.check_output([str(exe), str(mode), str(level), str(count), str(FPS), str(int(pet))])
            assert len(raw) == count * 128 * 64 * 3
            width, height = COLUMNS * 128, math.ceil(count / COLUMNS) * 64
            atlas = bytearray(width * height * 3)
            for frame in range(count):
                for y in range(64):
                    src = (frame * 64 + y) * 128 * 3
                    dst = ((frame // COLUMNS * 64 + y) * width + frame % COLUMNS * 128) * 3
                    atlas[dst:dst + 128 * 3] = raw[src:src + 128 * 3]
            data = png(width, height, atlas)
            (OUT / f'{name}.png').write_bytes(data)
            manifest['sequences'][name] = dict(file=f'{name}.png', frames=count, mode=mode, level=level,
                pet=pet, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            print(f'{name}: {count} original C frames, {len(data)} bytes', flush=True)
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
