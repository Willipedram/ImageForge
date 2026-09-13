# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
for package in ("PIL", "paramiko", "pymysql", "psutil"):
    hiddenimports += collect_submodules(package)
hiddenimports += ["win32cred", "win32timezone"]

a = Analysis(
    ["app/main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [], name="ImageOptimizer", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, console=False,
)
