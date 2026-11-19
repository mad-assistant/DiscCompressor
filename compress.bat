@echo off
rem Drag and drop one or more videos onto this file to compress them below 10 MB.
rem For another size, copy this file and change the last line to: python "%~dp0compress.py" --size 20 %*
python "%~dp0compress.py" %*
pause
