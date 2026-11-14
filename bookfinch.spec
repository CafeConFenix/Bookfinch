# -*- mode: python ; coding: utf-8 -*-
# Portable folder (recommended): dist/Bookfinch/Bookfinch.exe
# Starts faster and triggers fewer antivirus false positives than a single .exe.
import os

ICON = os.path.join(SPECPATH, "assets", "bookfinch.ico")
VERSION = os.path.join(SPECPATH, "version_info.txt")

a = Analysis(
    ["app.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=["bookfinch.ui", "bookfinch.icon"],
    excludes=["unittest", "pydoc", "doctest", "sqlite3", "xmlrpc"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Bookfinch",
    debug=False, strip=False,
    upx=False,                      # UPX increases antivirus false positives
    console=False,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION if os.path.exists(VERSION) else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Bookfinch")
