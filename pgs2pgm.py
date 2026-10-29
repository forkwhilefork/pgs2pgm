#!/usr/bin/env python3
"""Decode a raw PGS (.sup) subtitle stream into one grayscale PGM per cue.

PGS carries each subtitle as a run-length-encoded bitmap of palette indices
plus its own presentation timestamp, so nothing here has to seek, decode
video, or composite onto a frame -- the failure mode that silently produced
blank frames when this went through ffmpeg's overlay path.

usage: pgs2pgm.py <in.sup> <outdir>
"""
import os, sys

SEG_PDS, SEG_ODS, SEG_PCS, SEG_WDS, SEG_END = 0x14, 0x15, 0x16, 0x17, 0x80


def segments(data):
    i, n = 0, len(data)
    while i + 13 <= n:
        if data[i:i + 2] != b'PG':
            raise ValueError('bad segment magic at offset %d' % i)
        pts = int.from_bytes(data[i + 2:i + 6], 'big')
        typ = data[i + 10]
        ln = int.from_bytes(data[i + 11:i + 13], 'big')
        yield pts, typ, data[i + 13:i + 13 + ln]
        i += 13 + ln
    if i != len(data):
        raise ValueError('trailing %d bytes' % (len(data) - i))


def parse_pcs(p):
    objs = []
    n = p[10]
    i = 11
    for _ in range(n):
        oid = int.from_bytes(p[i:i + 2], 'big')
        cropped = p[i + 3] & 0x40
        x = int.from_bytes(p[i + 4:i + 6], 'big')
        y = int.from_bytes(p[i + 6:i + 8], 'big')
        objs.append((oid, x, y))
        i += 16 if cropped else 8
    return {'palette_id': p[9], 'objects': objs}


def parse_pds(p, palettes):
    pid = p[0]
    table = list(palettes.get(pid, [(0, 0, 0, 0)] * 256))
    i = 2
    while i + 5 <= len(p):
        table[p[i]] = (p[i + 1], p[i + 2], p[i + 3], p[i + 4])
        i += 5
    palettes[pid] = table


def parse_ods(ods_list):
    """Reassemble object fragments. A big bitmap is split over several ODS."""
    out, cur = {}, None
    for p in ods_list:
        oid = int.from_bytes(p[0:2], 'big')
        seq = p[3]
        if seq & 0x80:
            w = int.from_bytes(p[7:9], 'big')
            h = int.from_bytes(p[9:11], 'big')
            cur = [oid, w, h, bytearray(p[11:])]
        elif cur is not None:
            cur[3] += p[4:]
        if seq & 0x40 and cur is not None:
            out[cur[0]] = (cur[1], cur[2], bytes(cur[3]))
            cur = None
    return out


def decode_rle(data, width, height):
    """PGS RLE -> palette indices, one byte per pixel."""
    img = bytearray(width * height)
    x = y = i = 0
    n = len(data)
    while i < n and y < height:
        b = data[i]; i += 1
        if b:
            if x < width:
                img[y * width + x] = b
            x += 1
            continue
        if i >= n:
            break
        b2 = data[i]; i += 1
        if b2 == 0:
            y += 1; x = 0; continue
        kind = b2 & 0xC0
        if kind == 0x00:
            run, color = b2 & 0x3F, 0
        elif kind == 0x40:
            run = ((b2 & 0x3F) << 8) | data[i]; i += 1; color = 0
        elif kind == 0x80:
            run = b2 & 0x3F; color = data[i]; i += 1
        else:
            run = ((b2 & 0x3F) << 8) | data[i]; i += 1
            color = data[i]; i += 1
        if color and x < width:
            end = min(x + run, width)
            off = y * width
            img[off + x:off + end] = bytes([color]) * (end - x)
        x += run
    return img


def main(sup_path, outdir):
    os.makedirs(outdir, exist_ok=True)
    data = open(sup_path, 'rb').read()
    palettes = {}
    pcs = None
    pds_seen = []
    ods_seen = []
    pts = 0
    written = 0
    index = []

    for seg_pts, typ, payload in segments(data):
        if typ == SEG_PCS:
            pcs, pds_seen, ods_seen, pts = parse_pcs(payload), [], [], seg_pts
        elif typ == SEG_PDS:
            pds_seen.append(payload)
        elif typ == SEG_ODS:
            ods_seen.append(payload)
        elif typ == SEG_END:
            if pcs is None or not pcs['objects']:
                continue                      # a clear, not a cue
            for p in pds_seen:
                parse_pds(p, palettes)
            objs = parse_ods(ods_seen)
            placed = [(x, y, objs[o]) for o, x, y in pcs['objects'] if o in objs]
            if not placed:
                continue
            x0 = min(x for x, _, _ in placed)
            y0 = min(y for _, y, _ in placed)
            x1 = max(x + o[0] for x, _, o in placed)
            y1 = max(y + o[1] for _, y, o in placed)
            W, H = x1 - x0, y1 - y0
            if W <= 0 or H <= 0:
                continue
            # alpha-composite the palette onto black, as a 256-byte LUT
            table = palettes.get(pcs['palette_id'], [(0, 0, 0, 0)] * 256)
            lut = bytes(min(255, (e[0] * e[3]) // 255) for e in table)
            canvas = bytearray(W * H)
            for x, y, (w, h, rle) in placed:
                idx = decode_rle(rle, w, h)
                ox, oy = x - x0, y - y0
                for row in range(h):
                    src = idx[row * w:(row + 1) * w]
                    dst = (oy + row) * W + ox
                    canvas[dst:dst + w] = src.translate(lut)
            secs = pts / 90000.0
            name = '%05d_%09.3f.pgm' % (written, secs)
            with open(os.path.join(outdir, name), 'wb') as f:
                f.write(b'P5\n%d %d\n255\n' % (W, H))
                f.write(bytes(canvas))
            index.append('%s\t%.3f\t%dx%d' % (name, secs, W, H))
            written += 1

    with open(os.path.join(outdir, 'index.tsv'), 'w') as f:
        f.write('\n'.join(index) + '\n')
    print('%d cues decoded' % written)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
