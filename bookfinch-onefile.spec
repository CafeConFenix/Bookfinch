# -*- mode: python ; coding: utf-8 -*-
# A single portable Bookfinch.exe:  dist/Bookfinch.exe
# (it unpacks into %TEMP% on every launch: slower, and Windows Defender watches it more closely)
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
    pyz, a.scripts, a.binaries, a.datas, [],
    name="Bookfinch",
    debug=False, strip=False, upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION if os.path.exists(VERSION) else None,
)
