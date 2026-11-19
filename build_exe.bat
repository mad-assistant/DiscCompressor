@echo off
rem Builds dist\DiscCompressor\DiscCompressor.exe, a standalone app you can share (no Python needed).
rem Add --zip to also make dist\DiscCompressor.zip, or --no-ffmpeg for a much smaller build.
python "%~dp0build_exe.py" %*
pause
