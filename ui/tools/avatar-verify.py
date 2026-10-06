#!/usr/bin/env python3
"""Verify the original-C atlases and browser grid mapping; emit a visual contact sheet."""
from pathlib import Path
import hashlib
import importlib.util
import json
import struct
import subprocess
import tempfile
import zlib

UI = Path(__file__).resolve().parents[1]
OUT = UI / 'public' / 'esp32-avatar'
spec = importlib.util.spec_from_file_location('avatar_build', UI / 'tools' / 'avatar-build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def read_png(path):
    data, pos, compressed = path.read_bytes(), 8, bytearray()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    while pos < len(data):
        length = struct.unpack_from('>I', data, pos)[0]
        kind, payload = data[pos+4:pos+8], data[pos+8:pos+8+length]
        assert zlib.crc32(kind + payload) == struct.unpack_from('>I', data, pos+8+length)[0]
        if kind == b'IHDR':
            width, height, depth, color, *_ = struct.unpack('>IIBBBBB', payload)
            assert depth == 8 and color == 2
        if kind == b'IDAT': compressed += payload
        pos += length + 12
    decoded = zlib.decompress(compressed)
    rows = []
    for y in range(height):
        start = y * (width * 3 + 1)
        assert decoded[start] == 0
        rows.append(decoded[start+1:start+1+width*3])
    return width, height, b''.join(rows)


def frame_pixels(atlas, frame, size):
    width, _, data = atlas
    x0, y0 = frame % 16 * 128, frame // 16 * 64
    mapping = [(i*64//size) | (128 if size >= 192 and (i+1)*64//size != i*64//size else 0) for i in range(size)]
    result = bytearray(size*size*3)
    for y, ym in enumerate(mapping):
        for x, xm in enumerate(mapping):
            src = ((y0+(ym&127))*width+x0+(xm&127)+(64 if (xm|ym)&128 else 0))*3
            dst = (y*size+x)*3
            result[dst:dst+3] = data[src:src+3]
    return result


manifest = json.loads((OUT / 'manifest.json').read_text())
atlases = {}
decoded_bytes = 0
for name, entry in manifest['sequences'].items():
    path = OUT / entry['file']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']
    atlases[name] = read_png(path)
    width, height, _ = atlases[name]
    decoded_bytes += width * height * 4

with tempfile.TemporaryDirectory(prefix='muse-avatar-check-') as temp:
    exe = Path(temp) / 'verify'
    subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-I', str(OUT/'source'),
                    str(UI/'tools'/'avatar-verify.c'), '-lm', '-o', str(exe)], check=True)
    for size in (64, 191, 192, 319, 466):
        for level_index in (0, 4):
            native = subprocess.check_output([str(exe), str(size), '21', str(level_index/4)])
            reproduced = frame_pixels(atlases[f'speaking-{level_index}'], 21, size)
            assert native == reproduced, (size, level_index)

names = ['idle', 'listening-0', 'thinking', 'speaking-0', 'speaking-1', 'speaking-4', 'happy', 'boot', 'error']
sheet = bytearray(768 * 768 * 3)
for i, name in enumerate(names):
    frame = frame_pixels(atlases[name], 21 if name != 'happy' else 10, 256)
    for y in range(256):
        dst = ((i//3*256+y)*768+i%3*256)*3
        sheet[dst:dst+256*3] = frame[y*256*3:(y+1)*256*3]
(UI/'tools'/'avatar-original-c-preview.png').write_bytes(build.png(768, 768, sheet))
result = dict(result='PASS', originalCSilentMouthFrames=160,
    exactNativeScalerComparisons=10, atlasFiles=len(atlases),
    totalFrames=sum(v['frames'] for v in manifest['sequences'].values()),
    maximumDecodedAtlasRGBABytes=decoded_bytes,
    previewRows=[names[i:i+3] for i in range(0,9,3)])
(UI/'tools'/'avatar-verification.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result))
