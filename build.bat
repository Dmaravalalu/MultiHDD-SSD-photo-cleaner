@echo off
REM Build a single-file Windows executable for harddisk_cleaner.py.
REM Run from a Developer Command Prompt or any cmd.exe with Python on PATH.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

pyinstaller --onefile --noconsole ^
            --name HardDriveCleaner ^
            --collect-submodules pillow_heif ^
            --hidden-import PIL._tkinter_finder ^
            harddisk_cleaner.py

echo.
echo Build finished. Executable: dist\HardDriveCleaner.exe
