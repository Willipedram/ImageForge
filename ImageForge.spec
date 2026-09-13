# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hiddenimports = []
for package in ("PIL", "paramiko", "pymysql", "psutil"):
    hiddenimports += collect_submodules(package)
hiddenimports += ["win32cred", "win32timezone"]

# Runtime audit records package versions when a scan starts.  PyInstaller's
# module collection does not guarantee that the corresponding dist-info is
# included, so preserve it explicitly for every audited dependency.
datas = []
for distribution in ("Pillow", "PySide6", "paramiko", "PyMySQL", "psutil"):
    datas += copy_metadata(distribution)

a = Analysis(
    ["app/main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
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
