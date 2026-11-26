"""Cut a long video into 9:16 shorts with NVENC.

  python shorts.py clip.mp4                          auto-split on silence, render all
  python shorts.py clip.mp4 --list                   show segments, render nothing
  python shorts.py clip.mp4 --times 0:05-1:02 2:10-3:00
  python shorts.py clip.mp4 --mode crop --y 0.35     crop instead of blur-fill (0=top, 0.5=center)
  python shorts.py clip.mp4 --max 60 --min 20 --noise -30dB --pad 0.2

Output goes to <input>_shorts/ with a manifest.json listing every clip.
Falls back to libx264 if NVENC fails.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

W, H = 1080, 1920


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    if out.returncode != 0:
        raise SystemExit(f"ffprobe failed on {path}\n{out.stderr[-400:]}")
    return float(out.stdout.strip())


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.stdout.strip())


def parse_ts(text):
    parts = [float(p) for p in text.split(":")]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}{s:02d}"


def detect_speech(path, total, noise, min_silence):
    out = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", f"silencedetect=noise={noise}:d={min_silence}", "-f", "null", "-",
    ])
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", out.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: (-?[\d.]+)", out.stderr)]
    if len(ends) < len(starts):
        ends.append(total)

    silences = list(zip(starts, ends))
    speech, cursor = [], 0.0
    for s, e in silences:
        if s > cursor:
            speech.append((cursor, min(s, total)))
        cursor = max(cursor, e)
    if cursor < total:
        speech.append((cursor, total))
    return [(s, e) for s, e in speech if e - s > 0.3]


def build_segments(speech, total, min_dur, max_dur, pad):
    padded = [(max(0.0, s - pad), min(total, e + pad)) for s, e in speech]

    merged = []
    for s, e in padded:
        if merged and s - merged[-1][1] < 0.35:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    segments = []
    for s, e in merged:
        length = e - s
        if length < min_dur:
            if segments and (e - segments[-1][0]) <= max_dur:
                segments[-1] = (segments[-1][0], e)
            continue
        if length <= max_dur:
            segments.append((s, e))
            continue
        parts = int(length // max_dur) + 1
        step = length / parts
        segments.extend((s + i * step, s + (i + 1) * step) for i in range(parts))
    return segments


def video_filter(mode, y):
    if mode == "crop":
        return (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H}:(iw-{W})/2:(ih-{H})*{y},setsar=1,format=yuv420p[v]"
        )
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=24[bgb];"
        f"[fg]scale={W}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
    )


def render(src, start, end, out_path, mode, y, audio):
    duration = end - start
    graph = video_filter(mode, y)
    maps = ["-map", "[v]"]
    if audio:
        graph += ";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        maps += ["-map", "[a]"]
        acodec = ["-c:a", "aac", "-b:a", "192k"]
    else:
        acodec = ["-an"]

    def attempt(venc_args):
        return run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
            "-filter_complex", graph, *maps,
            *venc_args, *acodec,
            "-shortest", "-movflags", "+faststart", str(out_path),
        ])

    result = attempt(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"])
    if result.returncode == 0:
        return "nvenc"
    result = attempt(["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"])
    if result.returncode != 0:
        raise SystemExit(f"render failed on {start:.1f}-{end:.1f}\n{result.stderr[-600:]}")
    return "x264"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--list", action="store_true", help="show segments without rendering")
    ap.add_argument("--times", nargs="*", help="explicit ranges, e.g. 0:05-1:02")
    ap.add_argument("--mode", choices=["blur", "crop"], default="blur")
    ap.add_argument("--y", type=float, default=0.5, help="crop vertical offset, 0=top 1=bottom")
    ap.add_argument("--min", type=float, default=20.0, dest="min_dur")
    ap.add_argument("--max", type=float, default=75.0, dest="max_dur")
    ap.add_argument("--noise", default="-30dB")
    ap.add_argument("--min-silence", type=float, default=0.6)
    ap.add_argument("--pad", type=float, default=0.15)
    args = ap.parse_args()

    src = args.input.resolve()
    if not src.exists():
        raise SystemExit(f"not found: {src}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not on PATH")

    total = probe_duration(src)
    audio = has_audio(src)

    if args.times:
        segments = []
        for spec in args.times:
            a, b = spec.split("-")
            segments.append((parse_ts(a), parse_ts(b)))
    else:
        segments = build_segments(detect_speech(src, total, args.noise, args.min_silence),
                                  total, args.min_dur, args.max_dur, args.pad)

    if not segments:
        raise SystemExit("no segments found — lower --min or raise --noise")

    outdir = src.with_name(f"{src.stem}_shorts")
    outdir.mkdir(exist_ok=True)

    manifest = []
    for i, (start, end) in enumerate(segments, 1):
        name = f"{src.stem}_{i:02d}_{mmss(start)}-{mmss(end)}.mp4"
        label = f"[{i}/{len(segments)}] {start:7.2f} - {end:7.2f}  ({end - start:5.1f}s)  {name}"
        if args.list:
            print(label)
        else:
            encoder = render(src, start, end, outdir / name, args.mode, args.y, audio)
            print(f"{label}  {encoder}")
        manifest.append({
            "index": i, "start": round(start, 2), "end": round(end, 2),
            "duration": round(end - start, 2), "file": name,
        })

    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{len(manifest)} clip(s) -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
