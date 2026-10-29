# pgs2pgm

Decode a raw HDMV PGS subtitle stream (`.sup`) into one grayscale PGM image
per cue, with timestamps. Pure Python, no dependencies.

PGS — the subtitle format on Blu-ray — stores each subtitle as a run-length
encoded bitmap rather than text, so reading one means OCR, and OCR means
getting the bitmaps out first.

## Why not just use ffmpeg

ffmpeg decodes PGS fine, but it has no muxer that writes a decoded subtitle
bitmap to an image file: the `image2` muxer needs a *video* stream. The usual
workaround is to composite the subtitle onto a generated video frame:

```sh
ffmpeg -ss $((T-2)) -i in.mkv \
  -filter_complex "color=c=black:s=1920x1080:r=2[bg];[bg][0:s:0]overlay[o]" \
  -map "[o]" -t 5 -f image2 out_%02d.png
```

That works, but it has a sharp edge. PGS cues are not keyframe-indexed — a
cue's bitmap exists in exactly one packet. Seek past that packet and the
decoder never receives it, every frame renders black, and you get a confident
stream of empty results rather than an error. You have to seek *before* each
cue and then work out which of the resulting frames actually has the subtitle
on it. It is also slow: one seek and decode per cue.

Reading the `.sup` directly sidesteps all of it. Each cue carries its own
bitmap and its own presentation timestamp, so there is nothing to seek and no
decoder state to resynchronise.

## Usage

Extract the raw stream (no decoding, one sequential read):

```sh
ffmpeg -i input.mkv -map 0:s:0 -c copy subs.sup
```

Decode it to images:

```sh
python3 pgs2pgm.py subs.sup outdir/
```

You get `outdir/00000_00008.425.pgm` ... one per cue, named by index and
presentation time in seconds, plus an `index.tsv` of `name, seconds, WxH`.

PGM is uncompressed and understood by Leptonica, so Tesseract reads it
directly:

```sh
for f in outdir/*.pgm; do
  echo "$f: $(tesseract "$f" - --psm 6 2>/dev/null)"
done
```

The images are the subtitle's own bounding box — typically a few hundred
pixels tall rather than a full frame — so OCR is faster than it would be on
composited video frames.

## Format notes

A `.sup` is a flat sequence of segments, each with a 13-byte header: the
magic `PG`, a 32-bit presentation timestamp in 90kHz ticks, a 32-bit decode
timestamp, a one-byte type and a 16-bit payload length.

| Type   | Name | Carries                                  |
|--------|------|------------------------------------------|
| `0x14` | PDS  | Palette, as YCrCb + alpha                |
| `0x15` | ODS  | The RLE bitmap                           |
| `0x16` | PCS  | Composition: which objects, placed where |
| `0x17` | WDS  | Window rectangles                        |
| `0x80` | END  | End of display set                       |

Cues come in pairs: a PCS listing objects puts a subtitle up, and a later PCS
with no objects takes it down. Only the first kind produces an image here.

Two details that are easy to miss:

- A bitmap larger than a segment is split across several ODS, flagged first
  and last in the sequence byte, and has to be reassembled before decoding.
- Palettes persist across display sets within an epoch, so a PDS has to update
  a kept table rather than replace it.

The RLE itself is byte-oriented. A non-zero byte is one pixel of that palette
index. A zero byte introduces a run, where the next byte's top two bits select
one of four encodings: short or long run length, in the background colour or
in a given colour.

Output is alpha-composited onto black — `Y * A / 255` — which gives light text
on a dark field. Tesseract handles that without preprocessing.

## Limitations

- Palette indices are flattened to luminance; colour is discarded. Fine for
  OCR, not if you want to re-render the subtitle faithfully.
- Object cropping (the `cropped` flag in a PCS) is parsed so placement stays
  correct, but the crop rectangle itself is not applied.
- Forced-subtitle flags are not surfaced.
