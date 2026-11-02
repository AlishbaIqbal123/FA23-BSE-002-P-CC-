@echo off
setlocal
cd /d "%~dp0"
python -m pip install pyinstaller
set EXC=--exclude-module PySide6 --exclude-module PySide2 --exclude-module PyQt5 --exclude-module shiboken6 --exclude-module pygame --exclude-module matplotlib --exclude-module tkinter
python -m PyInstaller --noconfirm --clean --onefile --windowed --name RDO-Server --collect-data qdarktheme %EXC% server\app.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name RDO-Client --collect-data qdarktheme %EXC% client\app.py
echo.
echo Done. EXEs are in the dist folder.
