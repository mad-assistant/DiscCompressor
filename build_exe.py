"""
Build a standalone DiscCompressor folder (no Python needed on the target PC) with PyInstaller.

    python build_exe.py              bundles ffmpeg: works on any Windows PC (folder is ~470 MB)
    python build_exe.py --no-ffmpeg  small build; the PC must have ffmpeg installed
    python build_exe.py --zip        also writes dist/DiscCompressor.zip, ready to share

Output: dist/DiscCompressor/DiscCompressor.exe (keep it with the rest of that folder).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
APP = DIST / "DiscCompressor"
sys.path.insert(0, str(HERE))
import compress  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--no-ffmpeg", action="store_true", help="don't bundle ffmpeg/ffprobe")
    ap.add_argument("--zip", action="store_true", help="also create dist/DiscCompressor.zip")
    args = ap.parse_args()

    ffmpeg_files = []
    if not args.no_ffmpeg:
        for tool in ("ffmpeg", "ffprobe"):
            path = compress.find_tool(tool)
            if not path:
                sys.exit(f"{tool} not found. Install it (winget install Gyan.FFmpeg) or use --no-ffmpeg.")
            ffmpeg_files.append(Path(path).resolve())

    print("Installing/updating PyInstaller and tkinterdnd2...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--upgrade",
                    "pyinstaller", "tkinterdnd2"], check=True)

    print("Building...")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--windowed", "--name", "DiscCompressor",
        "--collect-all", "tkinterdnd2",            # drag & drop needs its tkdnd DLLs and Tcl files
        "--distpath", str(DIST), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
        str(HERE / "gui.pyw"),
    ], check=True)

    # ffmpeg goes next to the exe (compress.find_tool looks in <exe folder>/ffmpeg first).
    # Copying it afterwards is much faster than letting PyInstaller scan 450 MB of binaries.
    if ffmpeg_files:
        (APP / "ffmpeg").mkdir(exist_ok=True)
        for f in ffmpeg_files:
            print(f"Bundling {f.name}...")
            shutil.copy2(f, APP / "ffmpeg" / f.name)

    shutil.copy2(HERE / "README.md", APP / "README.md")   # the app's Help window reads it

    if args.zip:
        print("Zipping...")
        shutil.make_archive(str(DIST / "DiscCompressor"), "zip", DIST, "DiscCompressor")

    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file()) / 1e6
    print(f"\nDone: {APP / 'DiscCompressor.exe'}  ({size:.0f} MB folder)")
    if args.zip:
        print(f"Zip:  {DIST / 'DiscCompressor.zip'}")


if __name__ == "__main__":
    main()
