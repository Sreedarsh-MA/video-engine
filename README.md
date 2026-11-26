# video-engine

Two command-line tools for getting finished clips out of long recordings: one cuts
video into 9:16 shorts, the other mixes a voice-over under a music bed. Both drive
FFmpeg directly, so there is no editor GUI, no Python dependencies and no build step.

## shorts.py

Splits a long video into vertical clips.

```
python shorts.py clip.mp4                       # split on silence, render every segment
python shorts.py clip.mp4 --list                # show the segments, render nothing
python shorts.py clip.mp4 --times 0:05-1:02 2:10-3:00
python shorts.py clip.mp4 --mode crop --y 0.35  # crop instead of blur-filling (0 = top, 0.5 = centre)
python shorts.py clip.mp4 --max 60 --min 20 --noise -30dB --pad 0.2
```

Output lands in `<input>_shorts/` with a `manifest.json` listing every clip and its
time range. The default mode keeps the full frame and blur-fills the sides, which is
usually the difference between a usable short and a cropped-off subject. It renders on
NVENC when the card is available and falls back to libx264 when it is not.

## mix.py

Places a voice-over on top of a music bed without the usual `amix` surprises.

```
python mix.py vo.mp3 bed.mp3 out.m4a
python mix.py vo.mp3 bed.mp3 out.mp4 --video clip.mp4     # mux straight into the video
python mix.py vo.mp3 bed.mp3 out.m4a --duck               # music dips under the voice
python mix.py vo.mp3 bed.mp3 out.m4a --target -14         # normalise to -14 LUFS
```

Three mix defaults were measured and changed rather than guessed:

- `amix` weights the inputs, which dropped the bed by about 8 dB. Fixed with `normalize=0`.
- `amix` negotiates a single sample rate, so a 44.1 kHz bed against a 24 kHz voice lost
  everything above 12 kHz. Both legs are resampled to 48 kHz first.
- `amix` also negotiates one channel layout, and a mono voice collapsed the stereo bed
  (L/R correlation 0.754 to 1.000). Both inputs are forced to stereo.

Every run prints the measured integrated loudness and true peak, because a gain change
that is not measured is a guess.

## Requirements

- Python 3.10 or newer
- FFmpeg on `PATH` (with `ffprobe`)
- NVENC is optional; without it `shorts.py` uses libx264

## Notes

Both scripts are pure standard library on the Python side and call FFmpeg with `-y`,
so an existing file at the output path is replaced. Point them at a new filename when
you want to keep an earlier cut.
