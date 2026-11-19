#!/usr/bin/env python3
"""
DiscCompressor - shrink videos below a target size (default 10 MB) using ffmpeg.

Uses two-pass encoding (H.264 or AV1) at a bitrate calculated from the video's
duration, so the output lands just under the size limit. When the budget is
tight it lowers audio bitrate, frame rate (to 30fps) and resolution, and it
retries with a lower bitrate if the result still comes out too big.

This module is both the engine (used by gui.pyw) and a command-line tool:
    python compress.py video.mp4
    python compress.py a.mp4 b.mov --size 20 --codec av1
    python compress.py clip.mkv -o small.mp4
    python compress.py talk.mp4 --audio mono --out-dir D:/Clips
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import namedtuple
from fractions import Fraction
from pathlib import Path

# status is "done", "skipped" (already small enough, nothing written) or "failed".
Result = namedtuple("Result", "status message")

MB = 1000 * 1000          # decimal MB: 10 MB = 10,000,000 bytes, also under 10 MiB
SAFETY = 0.96             # leave ~4% headroom for container overhead / rate-control drift
MAX_ATTEMPTS = 3
MIN_VIDEO_KBPS = 100      # below this, quality is unwatchable
FPS_CAP = 30
FPS_CAP_BELOW_KBPS = 4000  # only drop high-fps footage to 30fps when bits are scarce

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]
# x264 preset name -> SVT-AV1 preset number (lower = slower/better)
SVT_PRESETS = {"ultrafast": 12, "superfast": 11, "veryfast": 10, "faster": 9, "fast": 9,
               "medium": 8, "slow": 6, "slower": 5, "veryslow": 4}
CODECS = {
    # name: (label, efficiency vs H.264 used when choosing resolution/fps)
    "h264": ("H.264", 1.0),
    "av1": ("AV1", 1.4),
}
AUDIO_MODES = ("keep", "mono", "none")
HDR_TRANSFERS ={"smpte2084", "arib-std-b67"}   # PQ (HDR10/Dolby Vision) and HLG
SDR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
GPU_EFFICIENCY = 0.9     # hardware encoders need a few more bits for the same quality
# GPU encoder families in order of preference, and their per-codec encoder names
GPU_FAMILIES = [("NVIDIA", "nvenc"), ("Intel", "qsv"), ("AMD", "amf")]
NVENC_PRESETS = {"ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p4",
                 "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7"}
AMF_QUALITY = {"ultrafast": "speed", "superfast": "speed", "veryfast": "speed", "faster": "speed",
               "fast": "balanced", "medium": "balanced", "slow": "quality", "slower": "quality",
               "veryslow": "quality"}

# Don't flash console windows when launched from the GUI on Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
FFMPEG_ENV = {**os.environ, "SVT_LOG": "1"}   # silence SVT-AV1's info banner


class Cancelled(Exception):
    pass


def bundled_dirs():
    """Folders that may hold an ffmpeg shipped with the app (packaged .exe or a copy next to it)."""
    dirs = [Path(__file__).resolve().parent / "ffmpeg"]
    if getattr(sys, "frozen", False):
        dirs.insert(0, Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ffmpeg")
        dirs.insert(1, Path(sys.executable).parent / "ffmpeg")
    return dirs


def find_tool(tool):
    """Locate ffmpeg/ffprobe: bundled copy first, then PATH, then the winget install location."""
    for d in bundled_dirs():
        exe = d / f"{tool}.exe"
        if exe.is_file():
            return str(exe)
    path = shutil.which(tool)
    if path:
        return path
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{tool}.exe"
    if links.is_file():
        return str(links)
    pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in pkgs.glob(f"Gyan.FFmpeg*/**/bin/{tool}.exe"):
        return str(exe)
    return None


def tools():
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg not found. Install it with `winget install Gyan.FFmpeg`.")
    return ffmpeg, ffprobe


_gpu_cache = None


def detect_gpu_encoders():
    """
    Return {codec: (encoder, vendor)} for hardware encoders that actually work on this PC.
    ffmpeg lists every encoder it was built with, so each one is test-run on a tiny clip.
    """
    global _gpu_cache
    if _gpu_cache is not None:
        return _gpu_cache
    ffmpeg, _ = tools()
    found = {}
    for codec in CODECS:
        for vendor, fam in GPU_FAMILIES:
            enc = f"{codec}_{fam}"
            try:
                r = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=size=640x360:rate=30", "-t", "0.5",
                     "-c:v", enc, "-f", "null", "-"],
                    capture_output=True, timeout=20, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0:
                found[codec] = (enc, vendor)
                break
    _gpu_cache = found
    return found


def parse_time(text):
    """'90', '1:30', '1:02:03.5' -> seconds. Empty -> None."""
    text = (text or "").strip()
    if not text:
        return None
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    if secs < 0:
        raise ValueError(text)
    return secs


def format_time(secs):
    """65.5 -> '1:05.5', 3725 -> '1:02:05'."""
    secs = round(max(0.0, secs), 1)
    whole = int(secs)
    tenth = round((secs - whole) * 10)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:02d}" + (f".{tenth}" if tenth else "")
    return f"{h}:{m:02d}:{s_txt}" if h else f"{m}:{s_txt}"


def _rotation(stream):
    """Rotation in degrees from the display matrix (modern) or rotate tag (older files)."""
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            return int(float(sd["rotation"]))
    return int(float(stream.get("tags", {}).get("rotate", 0) or 0))


def _fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            f = Fraction(stream.get(key, "0/0"))
            if f > 0:
                return float(f)
        except (ValueError, ZeroDivisionError):
            pass
    return 0.0


def probe(path):
    _, ffprobe = tools()
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)

    duration = float(data.get("format", {}).get("duration") or 0)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if video is None:
        raise RuntimeError("no video stream found")
    if duration <= 0:
        duration = float(video.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("could not determine video duration")

    # ffmpeg auto-rotates, so work with the dimensions as displayed (phones store
    # portrait video as landscape + a 90° rotation flag).
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if abs(_rotation(video)) % 180 == 90:
        w, h = h, w

    trc = video.get("color_transfer")
    return {
        "size": Path(path).stat().st_size,
        "duration": duration,
        "width": w,
        "height": h,
        "fps": _fps(video),
        "has_audio": audio is not None,
        "hdr": trc if trc in HDR_TRANSFERS else None,
    }


_filter_cache = None


def has_filter(name):
    """Whether this ffmpeg build includes a filter (e.g. zscale isn't in every build)."""
    global _filter_cache
    if _filter_cache is None:
        ffmpeg, _ = tools()
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        _filter_cache = {parts[1] for parts in map(str.split, out.splitlines()) if len(parts) > 2}
    return name in _filter_cache


def tonemap_filter(transfer):
    """HDR (PQ/HLG, BT.2020) -> SDR BT.709, so phone HDR clips don't look grey and washed out."""
    return (f"zscale=tin={transfer}:min=bt2020nc:pin=bt2020:t=linear:npl=100,"
            "format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def pick_audio_kbps(total_kbps, has_audio, mode="keep"):
    """Audio bitrate for the budget. Mono needs about half the bits for similar quality."""
    if not has_audio or mode == "none":
        return 0
    tiers = (64, 48, 40) if mode == "mono" else (128, 96, 64)
    if total_kbps > 1500:
        return tiers[0]
    if total_kbps > 600:
        return tiers[1]
    return tiers[2]


def grab_frame(src, t, box=(240, 160), hdr=None):
    """A PNG of the frame at t seconds, scaled to fit box (w, h), or None. Used for thumbnails."""
    ffmpeg, _ = tools()
    vf = f"scale=w={box[0]}:h={box[1]}:force_original_aspect_ratio=decrease"
    if hdr and has_filter("zscale"):
        vf += "," + tonemap_filter(hdr)
    for seek in (t, max(0.0, t - 1.0)):          # seeking right at the end can return nothing
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{seek:.3f}", "-i", str(src), "-frames:v", "1",
             "-vf", vf, "-f", "image2pipe", "-c:v", "png", "-"],
            capture_output=True, timeout=30, creationflags=NO_WINDOW, env=FFMPEG_ENV)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None


def pick_short_side(effective_kbps, src_short):
    """Choose the output's short side (720 = '720p', for landscape or portrait) for the bitrate."""
    if effective_kbps < 400:
        cap = 480
    elif effective_kbps < 1000:
        cap = 720
    elif effective_kbps < 2500:
        cap = 1080
    else:
        cap = src_short
    return min(cap, src_short) if src_short else cap


def run_ffmpeg(args, duration, on_progress, cancel):
    """Run ffmpeg, reporting progress (0..1) from its -progress output. Kills it on cancel."""
    ffmpeg, _ = tools()
    proc = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=NO_WINDOW, env=FFMPEG_ENV,
    )
    # Drain stderr in the background so a chatty error can't block the pipe.
    err_lines = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    t.start()

    for line in proc.stdout:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
        key, _, val = line.strip().partition("=")
        if key == "out_time_us" and val.isdigit():
            on_progress(min(int(val) / 1e6 / duration, 1.0))

    proc.wait()
    t.join(timeout=1)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if proc.returncode != 0:
        msg = [l.strip() for l in err_lines if l.strip() and not l.startswith("Svt[")]
        raise RuntimeError(msg[-1] if msg else f"ffmpeg exited with code {proc.returncode}")


def _video_args(plan, preset):
    vk = plan["video_kbps"]
    rate = ["-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.5)}k", "-bufsize", f"{vk * 2}k"]
    enc = plan["encoder"]
    if enc.endswith("_nvenc"):
        return ["-c:v", enc, "-preset", NVENC_PRESETS.get(preset, "p5"), "-tune", "hq",
                "-rc", "vbr", "-multipass", "fullres", "-spatial-aq", "1", *rate,
                "-pix_fmt", "yuv420p"]
    if enc.endswith("_qsv"):
        return ["-c:v", enc, "-preset", preset if preset in ("veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow") else "medium", *rate, "-pix_fmt", "nv12"]
    if enc.endswith("_amf"):
        return ["-c:v", enc, "-quality", AMF_QUALITY.get(preset, "balanced"),
                "-rc", "vbr_peak", *rate, "-pix_fmt", "yuv420p"]
    if enc == "libsvtav1":
        return ["-c:v", enc, "-preset", str(SVT_PRESETS.get(preset, 8)), "-b:v", f"{vk}k",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", preset, *rate, "-pix_fmt", "yuv420p"]


def encode(src, dst, plan, preset, on_progress, cancel):
    filters = []
    if plan["fps"]:
        filters.append(f"fps={plan['fps']}")
    if plan["scale"]:
        filters.append(plan["scale"])
    if plan["tonemap"]:
        filters.append(tonemap_filter(plan["tonemap"]))   # after scaling: less work per frame
    vf = ["-vf", ",".join(filters)] if filters else []
    tags = SDR_TAGS if plan["tonemap"] else []

    # Seeking before -i is fast, and accurate since we re-encode anyway.
    trim_in = ["-ss", f"{plan['start']:.3f}"] if plan["start"] else []
    trim_out = ["-t", f"{plan['duration']:.3f}"] if plan["trimmed"] else []

    ak = plan["audio_kbps"]
    audio = ["-c:a", "aac", "-b:a", f"{ak}k"] if ak else ["-an"]
    if ak and plan["mono"]:
        audio += ["-ac", "1"]
    dur = plan["duration"]
    final = [*audio, "-movflags", "+faststart", str(dst)]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            common = ["-y", *trim_in, "-i", str(src), *trim_out, *vf,
                      *_video_args(plan, preset), *tags]
            if plan["gpu"]:
                # GPU encoders hit the target well in one run (NVENC does its own 2-pass internally).
                run_ffmpeg(common + final, dur, on_progress, cancel)
                return
            common += ["-passlogfile", os.path.join(tmp, "ffpass")]
            # Pass 1 is analysis only; report it as the first half of the progress bar.
            run_ffmpeg(common + ["-pass", "1", "-an", "-f", "mp4", os.devnull],
                       dur, lambda f: on_progress(f * 0.5), cancel)
            run_ffmpeg(common + ["-pass", "2", *final],
                       dur, lambda f: on_progress(0.5 + f * 0.5), cancel)
    except BaseException:
        Path(dst).unlink(missing_ok=True)   # don't leave a half-written file behind
        raise


def make_plan(info, total_kbps, codec, encoder, gpu, start, duration, tonemap=None, audio="keep"):
    """Decide bitrates, frame rate and resolution for a given total bitrate."""
    audio_kbps = pick_audio_kbps(total_kbps, info["has_audio"], audio)
    video_kbps = total_kbps - audio_kbps
    effective = video_kbps * CODECS[codec][1] * (GPU_EFFICIENCY if gpu else 1.0)

    w, h = info["width"], info["height"]
    src_short = min(w, h) if w and h else h
    short = pick_short_side(effective, src_short)
    scale = None
    if src_short and short < src_short:
        short -= short % 2
        scale = f"scale=-2:{short}" if w >= h else f"scale={short}:-2"
    elif w % 2 or h % 2:
        # 4:2:0 video needs even dimensions; odd sizes (screen-region captures, GIFs) fail otherwise.
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        short -= short % 2

    fps = FPS_CAP if info["fps"] > FPS_CAP + 1 and effective < FPS_CAP_BELOW_KBPS else None
    out_fps = fps or info["fps"]

    return {"codec": codec, "encoder": encoder, "gpu": gpu,
            "start": start, "duration": duration, "trimmed": duration < info["duration"] - 0.01,
            "audio_kbps": audio_kbps, "video_kbps": video_kbps, "scale": scale, "fps": fps,
            "tonemap": tonemap, "mono": audio == "mono",
            "label": f"{short}p{round(out_fps) if out_fps else ''}" + (" HDR->SDR" if tonemap else "")}


def _trim_range(info, start, end):
    start = max(0.0, start or 0.0)
    end = info["duration"] if end is None else min(end, info["duration"])
    return start, end


def _total_kbps(target_mb, dur):
    return int(target_mb * MB * 8 * SAFETY / dur / 1000)


def min_size_mb(info, dur, audio="keep"):
    """Smallest target (MB, rounded up) that gives this clip watchable quality."""
    kbps = MIN_VIDEO_KBPS + pick_audio_kbps(0, info["has_audio"], audio)
    return -(-kbps * 1000 * dur // (8 * SAFETY * MB))


def estimate(info, target_mb, codec="h264", gpu=False, start=None, end=None,
             audio="keep", force=False):
    """
    Predict the result without encoding, from probe() info.
    Returns (fits, text), e.g. (True, '≈720p30'), (True, 'already small') or (False, 'needs 23 MB').
    """
    trimming = bool(start) or end is not None
    if info["size"] <= target_mb * MB and not force and not trimming:
        return True, "already small"
    start, end = _trim_range(info, start, end)
    dur = end - start
    if dur < 0.1:
        return False, "empty trim"
    plan = make_plan(info, _total_kbps(target_mb, dur), codec, "", gpu, start, dur,
                     info["hdr"], audio)
    if plan["video_kbps"] < MIN_VIDEO_KBPS:
        return False, f"needs {min_size_mb(info, dur, audio):g} MB"
    return True, "≈" + plan["label"]


def compress(src, dst, target_mb=10, preset="medium", force=False, codec="h264",
             gpu=False, start=None, end=None, audio="keep", info=None,
             log=print, on_progress=lambda f: None, cancel=None):
    """
    Compress src into dst below target_mb, optionally trimmed to [start, end] seconds.
    gpu=True uses a hardware encoder if one works for this codec (falls back to CPU otherwise).
    audio is "keep", "mono" or "none". info is an earlier probe(src) result, to skip re-probing.
    Returns a Result. Raises Cancelled if cancel (a threading.Event) is set.
    """
    src, dst = Path(src), Path(dst)
    limit = target_mb * MB
    trimming = bool(start) or end is not None

    if src.stat().st_size <= limit and not force and not trimming:
        return Result("skipped", f"already under {target_mb:g} MB")

    info = info or probe(src)
    start, end = _trim_range(info, start, end)
    if end - start < 0.1:
        return Result("failed", f"trim range {format_time(start)}–{format_time(end)} is empty "
                                f"(video is {format_time(info['duration'])} long)")
    dur = end - start
    total_kbps = _total_kbps(target_mb, dur)

    encoder = "libsvtav1" if codec == "av1" else "libx264"
    codec_label = CODECS[codec][0]
    if gpu:
        hw = detect_gpu_encoders().get(codec)
        if hw:
            encoder = hw[0]
            codec_label += " GPU"
        else:
            log(f"no GPU encoder for {codec_label} on this PC, using CPU")
            gpu = False

    tonemap = info["hdr"]
    if tonemap and not has_filter("zscale"):
        log("HDR video, but this ffmpeg has no zscale filter: colours may look washed out")
        tonemap = None

    wrote = False   # only delete dst on failure if we created it (-o may point at an existing file)

    def fail(msg):
        if wrote:
            dst.unlink(missing_ok=True)   # an over-limit file is useless and easy to send by mistake
        return Result("failed", msg)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        plan = make_plan(info, total_kbps, codec, encoder, gpu, start, dur, tonemap, audio)
        if plan["video_kbps"] < MIN_VIDEO_KBPS:
            return fail(f"too long ({format_time(dur)}) for {target_mb:g} MB, needs about "
                        f"{min_size_mb(info, dur, audio):g} MB. Trim it or raise the size.")

        mono = " mono" if plan["mono"] and plan["audio_kbps"] else ""
        log(f"attempt {attempt}: {codec_label} {plan['video_kbps']} kbps, "
            f"audio {plan['audio_kbps']} kbps{mono}, {plan['label']}")
        on_progress(0.0)

        encode(src, dst, plan, preset, on_progress, cancel)
        wrote = True

        out_size = dst.stat().st_size
        if out_size <= limit:
            return Result("done", f"{out_size / MB:.2f} MB · {plan['label']} · {codec_label}")

        # Overshot: scale the bitrate down proportionally, plus a little extra.
        total_kbps = int(total_kbps * (limit / out_size) * 0.95)
        log(f"result was {out_size / MB:.2f} MB, retrying lower")

    return fail(f"could not get under {target_mb:g} MB after {MAX_ATTEMPTS} attempts")


def default_output(src, out_dir=None):
    """<name>_compressed.mp4 next to the source (or in out_dir), adding (2), (3)... if taken."""
    src = Path(src)
    folder = Path(out_dir) if out_dir else src.parent
    dst = folder / f"{src.stem}_compressed.mp4"
    n = 2
    while dst.exists():
        dst = folder / f"{src.stem}_compressed ({n}).mp4"
        n += 1
    return dst


def main():
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    ap = argparse.ArgumentParser(description="Compress videos below a target size with ffmpeg.")
    ap.add_argument("inputs", nargs="+", help="video file(s) to compress")
    ap.add_argument("-s", "--size", type=float, default=10, help="target size in MB (default 10)")
    ap.add_argument("-o", "--output", help="output path (only with a single input; overwrites)")
    ap.add_argument("-d", "--out-dir", help="folder for the output files (default: next to each input)")
    ap.add_argument("-c", "--codec", default="h264", choices=list(CODECS),
                    help="h264 = plays everywhere (default), av1 = better quality per MB")
    ap.add_argument("-p", "--preset", default="medium", choices=PRESETS,
                    help="speed preset: faster = quicker, slower = better quality (default medium)")
    ap.add_argument("-a", "--audio", default="keep", choices=AUDIO_MODES,
                    help="keep (default), mono (frees bits for video) or none (remove audio)")
    ap.add_argument("-f", "--force", action="store_true",
                    help="re-encode even if the file is already under the target size")
    ap.add_argument("-g", "--gpu", action="store_true",
                    help="use the graphics card's encoder (much faster, slightly lower quality)")
    ap.add_argument("--start", help="trim: start time, e.g. 1:05 or 65")
    ap.add_argument("--end", help="trim: end time, e.g. 1:35 or 95")
    args = ap.parse_args()

    if args.output and len(args.inputs) > 1:
        ap.error("--output can only be used with a single input file")
    try:
        start, end = parse_time(args.start), parse_time(args.end)
    except ValueError:
        ap.error("times must look like 90, 1:30 or 1:02:03")
    if args.out_dir:
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    try:
        tools()
    except RuntimeError as e:
        sys.exit(f"Error: {e}")

    def show_progress(f):
        print(f"\r  {f * 100:5.1f}%", end="", flush=True)

    failures = 0
    for inp in args.inputs:
        src = Path(inp)
        print(f"\n{src.name}")
        if not src.is_file():
            print("  file not found")
            failures += 1
            continue
        dst = Path(args.output) if args.output else default_output(src, args.out_dir)
        try:
            status, msg = compress(src, dst, args.size, args.preset, args.force, args.codec,
                                   gpu=args.gpu, start=start, end=end, audio=args.audio,
                                   log=lambda m: print(f"\r  {m}"), on_progress=show_progress)
        except (RuntimeError, OSError) as e:
            status, msg = "failed", f"error: {e}"
        where = f" -> {dst}" if status == "done" else ""
        print(f"\r  {'FAILED' if status == 'failed' else status}: {msg}{where}")
        failures += status == "failed"

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
