#!/usr/bin/env python3
"""
censor.py - mute or bleep chosen words in movies you own.

Pipeline:
  1. ffmpeg extracts mono 16 kHz audio (center channel if the track is 5.1+)
  2. faster-whisper transcribes with word-level timestamps (cached to JSON)
  3. words are matched against words.toml (same categories as the MouthWash app)
     and padded into time spans
  4. ffmpeg re-muxes: video/subs copied untouched, first audio track muted/bleeped

Usage:
  python censor.py "D:\\Movies\\Film.mkv"
  python censor.py "D:\\Movies" --bleep
  python censor.py "Film.mkv" --sound duck.wav   # play your own sound instead
  python censor.py "Film.mkv" --dry-run          # transcribe + report only
  python censor.py "Film.mkv" --model large-v3 --pad 0.2
  python censor.py "Film.mkv" --sexual-only      # presets: --sexual-only,
                                                 #   --sexual-profanity (default), --everything
  python censor.py "Film.mkv" --categories sexual,slurs,mild --add frack* --skip hell
  python censor.py --check "goddamn" --everything
  python censor.py --list-categories

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


# Same categories and presets as the MouthWash app (crates/mouthwash-core/src/categories.rs).
CATEGORIES = {
    "sexual": "Sexual words, crude anatomy and slurs.",
    "profanity": "Everyday swearing and insults.",
    "slurs": "Racial, ethnic and homophobic slurs.",
    "insults": "Name-calling and derogatory words about intelligence, disability or appearance.",
    "religious": "Religious names used as exclamations, e.g. \"Jesus!\" or \"goddamn\".",
    "mild": "Mild exclamations like jeez, gosh, darn and heck.",
}
PRESETS = {  # slurs are part of every preset
    "sexual_only": ["sexual", "slurs"],
    "sexual_profanity": ["sexual", "profanity", "slurs"],
    "everything": list(CATEGORIES),
}
DEFAULT_CATEGORIES = PRESETS["sexual_profanity"]


def default_wordlist():
    here = Path(__file__).resolve().parent
    for p in (here / "words.toml",
              here / "crates" / "mouthwash-app" / "assets" / "words.toml",
              here / "words.txt"):
        if p.is_file():
            return p
    return here / "words.toml"


def load_categories(path):
    """Return {category: [entries]}. A .toml file has a [categories] table like the
    app's words.toml; any other file is a plain list, one entry per line ('#' comments)."""
    path = Path(path)
    if path.suffix.lower() != ".toml":
        return {None: path.read_text(encoding="utf-8").splitlines()}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            sys.exit("reading words.toml needs Python 3.11+ (or `pip install tomli`)")
    with open(path, "rb") as f:
        cats = tomllib.load(f).get("categories", {})
    return {c: list(cats.get(c, [])) for c in CATEGORIES}


def parse_entry(entry):
    """('exact'|'prefix'|'suffix'|'contains', core) or None for blanks/comments.
    word = exact, word* = starts with, *word = ends with, *word* = anywhere inside."""
    e = entry.strip().lower()
    if not e or e.startswith("#"):
        return None
    lead, trail = e.startswith("*"), len(e) > 1 and e.endswith("*")
    core = e.strip("*")
    if not core:
        return None
    kind = {(True, True): "contains", (True, False): "suffix",
            (False, True): "prefix", (False, False): "exact"}[(lead, trail)]
    return kind, core


def build_wordlist(entries):
    wl = {"exact": set(), "prefix": [], "suffix": [], "contains": []}
    for e in entries:
        p = parse_entry(e)
        if p:
            kind, core = p
            if kind == "exact":
                wl["exact"].add(core)
            else:
                wl[kind].append(core)
    return wl


def active_entries(cats, enabled, add, skip):
    """Union of enabled categories plus --add words, minus --skip entries."""
    skipped = {s.strip().lower() for s in skip}
    lines = []
    for c, entries in cats.items():
        if c is None or c in enabled:
            lines += entries
    lines += add
    return [e for e in lines if e.strip().lower() not in skipped]


def norm(word):
    return re.sub(r"[^a-z']", "", word.lower()).strip("'")


def candidates(w):
    """The word, plus the word without a possessive 's ("god's" -> "god")."""
    out = [w]
    if w.endswith("'s") and len(w) > 2:
        out.append(w[:-2])
    return out


def is_masked(word):
    """Whisper masked it with asterisks ("f***", "s**t"): always censored.
    "*sigh*" is a stage direction, not a masked word."""
    word = word.strip()
    return not word.startswith("*") and re.search(r"[^\W\d_]\*", word) is not None


def entry_matches(entry, normalized):
    p = parse_entry(entry)
    if not p or not normalized:
        return False
    kind, core = p
    test = {"exact": lambda w: w == core, "prefix": lambda w: w.startswith(core),
            "suffix": lambda w: w.endswith(core), "contains": lambda w: core in w}[kind]
    return any(test(w) for w in candidates(normalized))


def is_hit(word, wl):
    if not (wl["exact"] or wl["prefix"] or wl["suffix"] or wl["contains"]):
        return False
    if is_masked(word):
        return True
    w = norm(word)
    if not w:
        return False
    return any(c in wl["exact"]
               or any(c.startswith(p) for p in wl["prefix"])
               or any(c.endswith(p) for p in wl["suffix"])
               or any(p in c for p in wl["contains"])
               for c in candidates(w))


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


def build_spans(words, wl, pad, duration_guard=2.0):
    spans = []
    for w in words:
        if is_hit(w["word"], wl):
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
def process(src, args, wl):
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

    spans = build_spans(words, wl, args.pad)
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


def parse_categories(text):
    cats = [c.strip().lower() for c in text.split(",") if c.strip()]
    bad = [c for c in cats if c not in CATEGORIES]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown category {', '.join(bad)} (choose from {', '.join(CATEGORIES)})")
    return cats


def check_word(word, cats, enabled, add, skip):
    """Say which entries catch a word and whether it's censored with these flags."""
    n = norm(word)
    skipped = {s.strip().lower() for s in skip}
    hits = [(c, e) for c, entries in cats.items() for e in entries if entry_matches(e, n)]
    hits += [("extra (--add)", e) for e in add if entry_matches(e, n)]
    for c, e in hits:
        if e.strip().lower() in skipped:
            state = "skipped (--skip)"
        elif c is None or c == "extra (--add)" or c in enabled:
            state = "on"
        else:
            state = "off with these flags"
        print(f"  {e!r} in {c or 'word list'}: {state}")
    if is_masked(word):
        print("  masked by whisper (has *): always censored")
    wl = build_wordlist(active_entries(cats, enabled, add, skip))
    if not hits and not is_masked(word):
        print("  not in the word list")
    print(f"{word!r} -> {'CENSORED' if is_hit(word, wl) else 'not censored'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="movie file or folder of movies")

    words = ap.add_argument_group(
        "words to filter",
        "Pick a preset or your own categories (default: Sexual + Profanity + Slurs). "
        f"Categories: {', '.join(CATEGORIES)}.")
    pick = words.add_mutually_exclusive_group()
    pick.add_argument("--sexual-only", dest="categories", action="store_const",
                      const=PRESETS["sexual_only"], help="preset: sexual words and slurs")
    pick.add_argument("--sexual-profanity", dest="categories", action="store_const",
                      const=PRESETS["sexual_profanity"],
                      help="preset: sexual words, slurs and everyday swearing (default)")
    pick.add_argument("--everything", dest="categories", action="store_const",
                      const=PRESETS["everything"],
                      help="preset: every category, incl. insults, religious and mild")
    pick.add_argument("--categories", type=parse_categories, metavar="LIST",
                      help="comma-separated categories, e.g. sexual,slurs,mild")
    words.add_argument("--add", action="append", default=[], metavar="WORD",
                       help="extra word to always censor (repeatable; word*, *word, *word* allowed)")
    words.add_argument("--skip", action="append", default=[], metavar="ENTRY",
                       help="stop censoring this list entry, written as listed, e.g. hell or damn* "
                            "(repeatable)")
    words.add_argument("--check", metavar="WORD",
                       help="show whether a word is censored with these flags, then exit")
    words.add_argument("--list-categories", action="store_true",
                       help="show each category and its word count, then exit")
    words.add_argument("--wordlist", default=str(default_wordlist()),
                       help="words.toml (categories) or a plain .txt list "
                            "(default: words.toml next to the script)")

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

    if not Path(args.wordlist).is_file():
        sys.exit(f"word list not found: {args.wordlist} "
                 "(put words.toml next to censor.py or use --wordlist)")
    cats = load_categories(args.wordlist)
    plain = None in cats
    enabled = args.categories or DEFAULT_CATEGORIES

    if args.list_categories:
        if plain:
            sys.exit(f"{args.wordlist} is a plain list with no categories")
        for c, desc in CATEGORIES.items():
            mark = "*" if c in enabled else " "
            print(f" {mark} {c:<10} {len(cats[c]):>4} words  {desc}")
        print("\n * = on with these flags")
        return
    if args.check:
        check_word(args.check, cats, enabled, args.add, args.skip)
        return
    if not args.path:
        ap.error("give a movie file or folder (or use --check / --list-categories)")

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    if args.sound and not Path(args.sound).is_file():
        sys.exit(f"sound file not found: {args.sound}")

    if plain:
        if args.categories:
            print(f"note: {Path(args.wordlist).name} has no categories, so the preset is ignored")
        print(f"filtering: every word in {Path(args.wordlist).name}")
    else:
        print("filtering: " + ", ".join(c.capitalize() for c in CATEGORIES if c in enabled))
    if args.add:
        print("  plus: " + ", ".join(args.add))
    if args.skip:
        print("  skipping: " + ", ".join(args.skip))
    wl = build_wordlist(active_entries(cats, enabled, args.add, args.skip))

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
            process(f, args, wl)
        except subprocess.CalledProcessError as e:
            print(f"  FAILED: {Path(str(e.cmd[0])).name} exited with code {e.returncode} "
                  f"(see the message above)")
        except Exception as e:  # keep going through a folder
            print(f"  FAILED: {e}")


if __name__ == "__main__":
    main()
