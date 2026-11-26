"""Mix a voiceover over a music bed.

Lifted from JARVIS VoiceStudio's bed_mix_filter, which documents three measured bugs:
  1. amix normalizes by weight/sum -> the bed played 8 dB down. Fixed with normalize=0.
  2. amix negotiates one sample rate -> a 44.1/48k bed was resampled to the voice's 24k
     and lost everything above 12 kHz. Fixed by resampling both legs to 48k first.
  3. amix negotiates one channel layout -> the mono voice collapsed the stereo bed.
     Measured L/R correlation went 0.754 -> 1.000. Fixed with aformat=stereo on both.

  python mix.py vo.mp3 bed.mp3 out.m4a
  python mix.py vo.mp3 bed.mp3 out.mp4 --video clip.mp4      mux into the video, stream copy
  python mix.py vo.mp3 bed.mp3 out.m4a --duck                music dips under the voice
  python mix.py vo.mp3 bed.mp3 out.m4a --target -14          normalize to -14 LUFS

Always prints measured LUFS and true peak afterwards, because a gain change you cannot
measure is a guess.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

SR = 48000
BED_GAIN = 0.9
VOICE_GAIN = 1.1


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise SystemExit(f"could not read duration of {path}\n{out.stderr[-300:]}")


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.stdout.strip())


def amix_normalize_ok():
    out = run(["ffmpeg", "-hide_banner", "-h", "filter=amix"])
    return "normalize" in (out.stdout or "")


def build_graph(dur, bed_gain, voice_gain, fade, duck, target, normalize):
    tail = f",loudnorm=I={target}:TP=-1.5:LRA=11" if target is not None else ""

    bed = (
        f"[1:a]aresample={SR},aformat=channel_layouts=stereo,volume={bed_gain:g},"
        f"atrim=0:{dur:.3f},asetpts=N/SR/TB"
    )
    if fade > 0:
        out_fade = fade * 2
        bed += f",afade=t=in:st=0:d={fade:g},afade=t=out:st={max(0.0, dur - out_fade):.3f}:d={out_fade:g}"
    bed += "[bed]"

    voice = f"[0:a]aresample={SR},aformat=channel_layouts=stereo,volume={voice_gain:g}"
    if duck:
        voice += ",asplit=2[vo][vsc]"
        graph = (
            f"{bed};{voice};"
            f"[bed][vsc]sidechaincompress=threshold=0.03:ratio=8:attack=10:release=300[bedd];"
            f"[bedd][vo]amix=inputs=2:duration=first:dropout_transition=2:"
            f"{'normalize=0' if normalize else 'volume=2'},alimiter=level=false:limit=0.98:latency=1{tail}[aout]"
        )
        return graph

    graph = (
        f"{bed};{voice}[vo];"
        f"[bed][vo]amix=inputs=2:duration=first:dropout_transition=2:"
        f"{'normalize=0' if normalize else 'volume=2'},alimiter=level=false:limit=0.98:latency=1{tail}[aout]"
    )
    return graph


def measure(path):
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"])
    text = out.stderr or ""
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", text)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", text)
    return (lufs[-1] if lufs else "?"), (peak[-1] if peak else "?")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("voice")
    ap.add_argument("music")
    ap.add_argument("out")
    ap.add_argument("--video", help="mux the mix onto this video (stream copy, no re-encode)")
    ap.add_argument("--bed-gain", type=float, default=BED_GAIN)
    ap.add_argument("--voice-gain", type=float, default=VOICE_GAIN)
    ap.add_argument("--fade", type=float, default=0.5, help="bed fade in seconds; out is 2x")
    ap.add_argument("--duck", action="store_true", help="compress the bed under the voice")
    ap.add_argument("--target", type=float, default=None, help="loudnorm target, e.g. -14")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not on PATH")

    voice, music, out = Path(args.voice), Path(args.music), Path(args.out)
    for p in (voice, music):
        if not p.exists():
            raise SystemExit(f"not found: {p}")
        if not has_audio(p):
            raise SystemExit(f"no audio stream in {p}")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(voice), "-stream_loop", "-1", "-i", str(music)]
    if args.video:
        video = Path(args.video)
        if not video.exists():
            raise SystemExit(f"not found: {video}")
        cmd += ["-i", str(video)]

    dur = duration(voice)
    graph = build_graph(dur, args.bed_gain, args.voice_gain, args.fade, args.duck, args.target,
                        amix_normalize_ok())

    suffix = out.suffix.lower()
    if suffix == ".mp3":
        acodec = ["-c:a", "libmp3lame", "-b:a", "256k"]
    elif suffix == ".wav":
        acodec = ["-c:a", "pcm_s16le"]
    else:
        acodec = ["-c:a", "aac", "-b:a", "192k"]

    cmd += ["-filter_complex", graph]
    # loudnorm resamples internally and leaks 96 kHz into the output unless pinned.
    cmd += ["-ar", str(SR)]
    if args.video:
        cmd += ["-map", "2:v:0", "-map", "[aout]", "-c:v", "copy", *acodec,
                "-movflags", "+faststart", "-shortest"]
    else:
        cmd += ["-map", "[aout]", *acodec]
    cmd += [str(out)]

    result = run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"mix failed\n{result.stderr[-800:]}")

    lufs, peak = measure(out)
    print(f"wrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"measured: {lufs} LUFS integrated, {peak} dBFS true peak")
    if args.target is not None:
        print(f"target was {args.target} LUFS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
