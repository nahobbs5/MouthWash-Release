#!/usr/bin/env python3
"""
censor.py - mute or bleep chosen words in movies you own.

Pipeline:
  1. ffmpeg extracts mono 16 kHz audio (center channel if the track is 5.1+)
  2. faster-whisper transcribes with word-level timestamps (cached to JSON)
  3. words are matched against words.txt and padded into time spans
  4. ffmpeg re-muxes: video/subs copied untouched, first audio track muted/bleeped

Usage:
  python censor.py "D:\\Movies\\Film.mkv"
  python censor.py "D:\\Movies" --bleep
  python censor.py "Film.mkv" --sound duck.wav   # play your own sound instead
  python censor.py "Film.mkv" --dry-run          # transcribe + report only
  python censor.py "Film.mkv" --model large-v3 --pad 0.2

Requires: ffmpeg + ffprobe on PATH, `pip install faster-whisper`
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def probe_audio(path):
    """Return (channels, sample_rate, layout) of the first audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels,sample_rate,channel_layout",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    streams = json.loads(out).get("streams", [])
    if not streams:
        raise RuntimeError("no audio stream found")
    st = streams[0]
    layout = st.get("channel_layout") or ""
    if layout in ("", "unknown"):
        layout = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(int(st["channels"]), "")
    return int(st["channels"]), int(st["sample_rate"]), layout


def extract_audio(src, wav, channels):
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0"]
    if channels >= 6:
        # dialogue lives in the center channel on 5.1/7.1 mixes
        cmd += ["-af", "pan=mono|c0=FC"]
    else:
        cmd += ["-ac", "1"]
    cmd += ["-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    run(cmd)


def load_wordlist(path):
    """One entry per line. '#' comments. Trailing * = prefix match."""
    exact, prefixes = set(), []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        if line.endswith("*"):
            prefixes.append(line[:-1])
        else:
            exact.add(line)
    return exact, tuple(prefixes)


def norm(word):
    return re.sub(r"[^a-z']", "", word.lower()).strip("'")


def is_hit(word, exact, prefixes):
    w = norm(word)
    if not w:
        return False
    return w in exact or (prefixes and w.startswith(prefixes))


def add_nvidia_dll_dirs():
    """Windows: make DLLs from `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` findable."""
    if sys.platform != "win32":
        return
    import site
    for base in site.getsitepackages():
        root = Path(base) / "nvidia"
        if root.is_dir():
            for b in root.glob("*/bin"):
                try:
                    os.add_dll_directory(str(b))
                except OSError:
                    pass
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def device_choices(requested):
    """Ordered (device, compute_type) attempts. 'auto' tries the GPU, then the CPU."""
    if requested == "cpu":
        return [("cpu", "int8")]
    if requested == "cuda":
        return [("cuda", "float16")]
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return [("cuda", "float16"), ("cpu", "int8")]
    except Exception:
        pass
    return [("cpu", "int8")]


def transcribe(wav, model_name, language, requested="auto"):
    from faster_whisper import WhisperModel
    add_nvidia_dll_dirs()
    attempts = device_choices(requested)
    for i, (device, compute) in enumerate(attempts):
        print(f"  model={model_name} device={device}")
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute)
            segments, _ = model.transcribe(
                str(wav),
                language=language,
                word_timestamps=True,
                vad_filter=False,                  # VAD can drop words under loud music
                condition_on_previous_text=False,  # reduces drift/hallucination loops
                beam_size=5,
            )
            words = []
            for seg in segments:
                for w in seg.words or []:
                    words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
            return words
        except Exception as e:
            if i + 1 < len(attempts):
                print(f"  {device} failed ({e}); falling back to {attempts[i + 1][0]}")
                continue
            raise


def build_spans(words, exact, prefixes, pad, duration_guard=2.0):
    spans = []
    for w in words:
        if is_hit(w["word"], exact, prefixes):
            start = max(0.0, w["start"] - pad)
            end = w["end"] + pad
            # guard against Whisper stretching a word across a long silence
            if end - start > duration_guard:
                start = end - duration_guard
            spans.append([start, end, w["word"]])
    spans.sort()
    merged = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
            merged[-1][2] += " / " + s[2]
        else:
            merged.append(s)
    return merged


def ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):d}:{int(m):02d}:{s:06.3f}"


def write_report(path, spans):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{len(spans)} spans\n\n")
        for a, b, label in spans:
            f.write(f"{ts(a)} -> {ts(b)}   {label}\n")


_FILTER_OPT = None


def filter_script_args(script):
    """ffmpeg 7+ wants `-/filter_complex FILE`; older builds want `-filter_complex_script FILE`.
    Newer builds (8+) removed the old option, so probe once with a tiny graph."""
    global _FILTER_OPT
    if _FILTER_OPT is None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("[0:a]anull[out]")
            probe = f.name
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=d=0.1",
                 "-/filter_complex", probe, "-map", "[out]", "-f", "null", "-"],
                capture_output=True)
            _FILTER_OPT = "-/filter_complex" if r.returncode == 0 else "-filter_complex_script"
        finally:
            Path(probe).unlink(missing_ok=True)
    return [_FILTER_OPT, script]


MUTE_GROUP = 10  # spans per volume filter; keeps each ffmpeg expression short


def mute_chain(spans):
    """Mute spans with a chain of small volume filters.
    One giant expression with hundreds of between() terms fails in newer ffmpeg."""
    parts = []
    for i in range(0, len(spans), MUTE_GROUP):
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in spans[i:i + MUTE_GROUP])
        parts.append(f"volume=enable='{expr}':volume=0")
    return ",".join(parts)


def decode_sound(path, sample_rate, volume):
    """Decode any audio file to mono 16-bit samples at the movie's sample rate."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sample_rate),
         "-f", "s16le", "-"], capture_output=True, check=True).stdout
    clip = array("h")
    clip.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        clip.byteswap()
    if volume != 1.0:
        clip = array("h", (max(-32768, min(32767, int(v * volume))) for v in clip))
    if not len(clip):
        raise RuntimeError(f"no audio found in {path}")
    return clip


def make_tone(n, sample_rate, freq=1000, amp=0.2):
    return array("h", (int(32767 * amp * math.sin(2 * math.pi * freq * i / sample_rate))
                       for i in range(n)))


def shape(seg, fade_in, fade_out):
    """Short linear fades so the replacement sound doesn't click at its edges."""
    out = array("h", seg)
    n = len(out)
    fi, fo = min(fade_in, n // 2), min(fade_out, n // 2)
    for i in range(fi):
        out[i] = int(out[i] * (i + 1) / fi)
    for i in range(fo):
        out[n - fo + i] = int(out[n - fo + i] * (1 - (i + 1) / fo))
    return out


def silence(nsamples):
    block = bytes(1 << 20)
    nbytes = nsamples * 2
    while nbytes > 0:
        k = min(nbytes, len(block))
        yield block if k == len(block) else block[:k]
        nbytes -= k


def replacement_chunks(spans, sample_rate, clip, fade_in, fade_out, fill_span):
    """Stream a mono 16-bit track that is silent except for the clip placed at each span.
    Built in Python and piped to ffmpeg, so the ffmpeg graph stays tiny at any span count."""
    pos = 0
    for a, b, _ in spans:
        start = max(pos, int(round(a * sample_rate)))
        n = int(round((b - a) * sample_rate))
        yield from silence(start - pos)
        seg = shape(clip[:n], fade_in, fade_out)
        if not fill_span:
            seg = seg[:n]
        yield (seg.tobytes() if sys.byteorder == "little" else
               array("h", seg).tobytes())
        pos = start + len(seg)


def apply_filter(src, dst, spans, sample_rate, layout, bleep, sound, sound_vol):
    mute = mute_chain(spans)
    feed = None
    if sound:
        clip = decode_sound(sound, sample_rate, sound_vol)
        feed = replacement_chunks(spans, sample_rate, clip,
                                  0, int(0.05 * sample_rate), False)
    elif bleep:
        longest = max(int(round((b - a) * sample_rate)) for a, b, _ in spans)
        feed = replacement_chunks(spans, sample_rate, make_tone(longest, sample_rate),
                                  int(0.005 * sample_rate), int(0.005 * sample_rate), True)

    if feed is not None:
        fmt = f"aformat=sample_rates={sample_rate}"
        if layout:
            fmt += f":channel_layouts={layout}"
        graph = (f"[0:a:0]{mute}[m];[1:a]{fmt}[d];"
                 f"[m][d]amix=inputs=2:duration=first:normalize=0[out]")
        inputs = ["-i", str(src), "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
                  "-i", "pipe:0"]
    else:
        graph = f"[0:a:0]{mute}[out]"
        inputs = ["-i", str(src)]

    # script file avoids Windows command-line length limits
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(graph)
        script = f.name
    try:
        cmd = ["ffmpeg", "-y", "-v", "error", "-stats", *inputs,
               *filter_script_args(script),
               "-map", "0:v?", "-map", "[out]", "-map", "0:s?",
               "-c:v", "copy", "-c:s", "copy",
               "-c:a", "aac", "-b:a", "448k",
               str(dst)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed is not None else None)
        try:
            if feed is not None:
                try:
                    for chunk in feed:
                        proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    pass  # ffmpeg exited early; its exit code is reported below
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            rc = proc.wait()
        except BaseException:
            proc.kill()
            raise
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
    finally:
        Path(script).unlink(missing_ok=True)


# ---------------------------------------------------------------- main flow
def process(src, args, exact, prefixes):
    print(f"\n== {src.name}")
    out_dir = Path(args.out_dir) if args.out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem} (clean).mkv"
    cache = out_dir / f"{src.stem}.words.json"
    report = out_dir / f"{src.stem}.censor.txt"

    if dst.exists() and not args.force and not args.dry_run:
        print("  output exists, skipping (use --force)")
        return

    channels, sample_rate, layout = probe_audio(src)

    if cache.exists() and not args.retranscribe:
        print("  using cached transcript")
        words = json.loads(cache.read_text(encoding="utf-8"))
    else:
        print("  extracting audio...")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            extract_audio(src, wav, channels)
            print("  transcribing (this is the slow part)...")
            words = transcribe(wav, args.model, args.language, args.device)
        cache.write_text(json.dumps(words), encoding="utf-8")

    spans = build_spans(words, exact, prefixes, args.pad)
    write_report(report, spans)
    print(f"  {len(spans)} spans flagged -> {report.name}")

    if args.dry_run:
        return
    if not spans:
        print("  nothing to censor, no output written")
        return

    print("  writing censored copy...")
    apply_filter(src, dst, spans, sample_rate, layout, args.bleep,
                 args.sound, args.sound_volume)
    print(f"  done -> {dst}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="movie file or folder of movies")
    ap.add_argument("--wordlist", default=str(Path(__file__).with_name("words.txt")))
    ap.add_argument("--model", default="medium.en",
                    help="whisper model (small.en, medium.en, large-v3...)")
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                    help="auto tries the GPU first and falls back to the CPU")
    ap.add_argument("--language", default="en")
    ap.add_argument("--pad", type=float, default=0.15, help="seconds padded each side")
    ap.add_argument("--bleep", action="store_true", help="1 kHz tone instead of silence")
    ap.add_argument("--sound", help="audio file (e.g. duck.wav/mp3) to play over each censored word")
    ap.add_argument("--sound-volume", type=float, default=1.0, help="volume multiplier for --sound")
    ap.add_argument("--out-dir", help="where to write outputs (default: next to source)")
    ap.add_argument("--dry-run", action="store_true", help="transcribe + report only")
    ap.add_argument("--force", action="store_true", help="overwrite existing output")
    ap.add_argument("--retranscribe", action="store_true", help="ignore cached transcript")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    if args.sound and not Path(args.sound).is_file():
        sys.exit(f"sound file not found: {args.sound}")

    exact, prefixes = load_wordlist(args.wordlist)
    p = Path(args.path)
    if p.is_dir():
        files = sorted(f for f in p.iterdir()
                       if f.suffix.lower() in VIDEO_EXTS and "(clean)" not in f.stem)
    else:
        files = [p]
    if not files:
        sys.exit("no video files found")

    for f in files:
        try:
            process(f, args, exact, prefixes)
        except subprocess.CalledProcessError as e:
            print(f"  FAILED: {Path(str(e.cmd[0])).name} exited with code {e.returncode} "
                  f"(see the message above)")
        except Exception as e:  # keep going through a folder
            print(f"  FAILED: {e}")


if __name__ == "__main__":
    main()
