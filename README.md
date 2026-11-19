# DiscCompressor

Shrinks videos so they fit under Discord's upload limit (or any size you choose), then puts the result on your clipboard so you can paste it straight into a chat.

## Quick start

1. Open the app: double-click **DiscCompressor.bat** (or **DiscCompressor.exe** if you're using the packaged version).
2. Drag one or more videos onto the window, or click the drop area to browse.
3. Wait for the row to turn green and say **Done**.
4. Click into Discord and press **Ctrl+V**. The compressed video is already on your clipboard.

The compressed file is saved as `<name>_compressed.mp4` next to the original, unless you've chosen a different folder.

## Other ways to add videos

- **Drag videos onto DiscCompressor.bat** (or the .exe) in Explorer.
- **Right-click videos in Explorer → Send to → DiscCompressor.** Turn this on with the **Add to "Send to" menu** checkbox.
- **Drop a folder** to add every video inside it. Files already named `_compressed` are ignored.
- If the window is already open, new files are added to the same window instead of opening a second one.
- Adding a file that's already waiting or compressing is ignored, so double-drops don't encode twice.

## Settings

All settings are remembered between sessions.

**Target size**: pick a preset or choose **Custom** and type any size in MB.

- **Discord (10 MB)**: the free Discord limit.
- **20 MB**
- **Nitro Basic (50 MB)**
- **Nitro (500 MB)**

**Codec**

- **H.264 (plays everywhere)**: the safe choice; previews in every Discord client.
- **AV1 (better quality)**: noticeably sharper at the same size, but slower to encode and some older phones can't play it.

**Speed**: **Fast** finishes quickest, **Best quality** looks best but takes longer. **Balanced** is a good default.

**Audio**

- **Keep audio**: normal stereo sound.
- **Mono audio**: uses about half the bits for sound, leaving more for the picture. Good for voice, gameplay and long clips.
- **No audio**: removes the sound entirely, giving all the space to video.

**Save to**: click **Change…** to put all compressed videos in one folder (handy when the originals are on a phone, a read-only drive or OneDrive). Click **Next to original** to go back to the default.

**Use graphics card**: encodes on your GPU (NVIDIA, Intel or AMD). Much faster, slightly lower quality at the same size. Greyed out if no usable GPU encoder was found.

**Re-encode files already under the size**: normally a video that already fits is skipped. Tick this to compress it anyway.

**Copy to clipboard when done**: puts the finished file on the clipboard, ready for Ctrl+V. If a video was skipped because it already fits, the original is copied instead.

**Notify when done**: when everything finishes while you're in another window, the taskbar button flashes and a Windows notification appears. Notifications only show if they're enabled in Windows (Settings → System → Notifications & actions). They may be labelled "Windows PowerShell".

**Start automatically**: start compressing as soon as files are added. Untick it to queue several files, trim them, then press **Start**.

## Reading the list

- **ORIGINAL**: the file's current size.
- **LENGTH**: how long the video is.
- **TRIM**: the part being kept, if you've trimmed it.
- **ESTIMATE**: what to expect before encoding starts:
  - **≈720p30** means it will come out at about 720p, 30 frames per second.
  - **already small** means it already fits and will be skipped (and the original copied).
  - **needs 26 MB** (orange) means it's too long to look acceptable at this size. Trim it, choose mono or no audio, or pick a bigger size.
- **STATUS**: Waiting, Queued, a percentage while compressing, then Done, Skipped, Failed or Cancelled.
- **RESULT**: the final size, resolution and codec, or the reason it failed.

The bar at the bottom shows progress for the current video, and the taskbar button shows progress for the whole batch.

## Right-click menu

Select one or more rows and right-click:

- **Retry**: run a failed or cancelled video again.
- **Retry at 20 MB / 50 MB…**: retry at the smallest preset that's big enough for that video.
- **Trim…**: keep only part of the video (see below).
- **Copy file**: put the compressed file on the clipboard again (also **Ctrl+C**).
- **Show in folder**: open Explorer with the file selected (also double-click a row).
- **Remove**: take it off the list (also the **Delete** key). Files on disk aren't deleted.

**Cancel current** stops the video that's compressing; no half-finished file is left behind. **Clear finished** tidies the list.

## Trimming

Select a video and click **Trim…** (or right-click → Trim…).

1. Type a **Start** and **End** time. Any of these formats work: `90`, `1:30`, `1:02:03.5`.
2. The pictures above each box show the first and last frame you'll keep, so you can check you're cutting in the right place.
3. Press **Apply** (or Enter). **Remove trim** goes back to the full video.

Trimming a video that's already finished compresses it again with the new trim. Shorter clips get more quality from the same size, so trimming is the best way to make a long video look good under 10 MB.

## Good to know

- Long videos are automatically lowered in resolution and frame rate to fit, and the app retries with a lower bitrate if the first attempt comes out slightly too big.
- If a video can't get under the limit, nothing is saved, so you can't accidentally send a file that's too big.
- HDR videos from iPhones and modern Android phones are converted to normal colour so they don't look grey and washed out on Discord (the result shows **HDR->SDR**).
- Odd-sized videos such as screen-region recordings and GIFs work fine.

## Troubleshooting

- **"ffmpeg missing"**: install it with `winget install Gyan.FFmpeg` in a terminal, then reopen the app. The packaged .exe includes its own ffmpeg.
- **Drag & drop doesn't work** (the drop area says to install something): run `pip install tkinterdnd2`. You can still click the drop area to browse.
- **Failed: too long**: see the ESTIMATE column for the size it needs; trim it, use mono or no audio, or right-click → Retry at a bigger size.
- **No notification**: check Windows notifications are on, and that Focus assist isn't hiding them. The taskbar button flash works either way.
- **Settings** are stored in `%APPDATA%\DiscCompressor\settings.json`. Delete that file to reset everything.

## Command line

`compress.py` does the same compression without the window:

```
python compress.py video.mp4
python compress.py a.mp4 b.mov --size 20 --codec av1
python compress.py clip.mkv --start 1:05 --end 1:35 -o highlight.mp4
python compress.py talk.mp4 --audio mono --out-dir D:\Clips
```

Options: `--size` (MB, default 10), `--codec h264|av1`, `--preset` (ultrafast … veryslow), `--audio keep|mono|none`, `--gpu`, `--force`, `--start`, `--end`, `-o` (output file, single input only) and `--out-dir`. Run `python compress.py --help` for details.

**compress.bat**: drag videos onto it to compress them to 10 MB in a console window. For another size, copy the file and add `--size 20` before `%*` on its last line.

## Building the standalone app

To share DiscCompressor with someone who doesn't have Python, run **build_exe.bat** (or `python build_exe.py`). It creates `dist\DiscCompressor\` containing **DiscCompressor.exe**. Send the whole folder; the .exe needs the files next to it.

- `--zip` also makes `dist\DiscCompressor.zip`, ready to send.
- `--no-ffmpeg` makes a much smaller build (the full one is about 470 MB, almost all ffmpeg), but the other PC must then have ffmpeg installed.

## Requirements (when running from source)

- Windows 10 or 11
- Python 3.10 or newer
- ffmpeg: `winget install Gyan.FFmpeg`
- Optional, for drag & drop: `pip install tkinterdnd2`
