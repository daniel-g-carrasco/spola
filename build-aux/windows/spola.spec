# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of Spola for Windows, under MSYS2 UCRT64 (the recipe of
# p7m-extractor and pdf-cropper). Run from the repository root:
#   python tools/compile_po.py && python tools/make_ico.py && pyinstaller build-aux/windows/spola.spec
# A spec file, not flags: the gi hook collects GTK 3 unless told otherwise.
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent
prefix = Path(sys.prefix)              # MSYS2: C:/msys64/ucrt64

datas = [(str(ROOT / 'data' / 'icons'), 'share/icons')]
binaries = []

if (ROOT / 'locale').is_dir():
    datas.append((str(ROOT / 'locale'), 'share/locale'))
else:
    print('WARNING: locale/ missing: run tools/compile_po.py first (English only)')

# rclone, the sync engine: an official release, checked by the CI
rclone = os.environ.get('SPOLA_RCLONE')
if not rclone or not Path(rclone).is_file():
    raise SystemExit('SPOLA_RCLONE must point to rclone.exe')
binaries.append((rclone, '.'))

# gdbus.exe lets GLib start a session bus, which GApplication needs to keep
# one Spola running (a second launch shows the window of the first)
gdbus = shutil.which('gdbus') or str(prefix / 'bin' / 'gdbus.exe')
if Path(gdbus).is_file():
    binaries.append((gdbus, '.'))
else:
    print('WARNING: gdbus.exe not found: every launch would start another Spola')

# CA bundle for Spola's own calls to Koofr (rclone uses Windows' store)
for ca in (prefix / 'etc' / 'ssl' / 'certs' / 'ca-bundle.crt', prefix / 'etc' / 'ssl' / 'cert.pem'):
    if ca.is_file():
        datas.append((str(ca), 'certs'))
        break
else:
    print('WARNING: no CA bundle found: the activity feed may fail on TLS')

a = Analysis(
    [str(Path(SPECPATH) / 'spola_main.py')],
    pathex=[str(ROOT / 'src')],
    binaries=binaries,
    datas=datas,
    hiddenimports=['spola.wintray', 'spola.win32', 'spola.winwatch', 'spola.minisign'],
    hooksconfig={'gi': {'module-versions': {'Gtk': '4.0', 'Gdk': '4.0', 'Adw': '1', 'GdkPixbuf': '2.0',
                                            'Graphene': '1.0', 'Gsk': '4.0'}}},
    excludes=['tkinter'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='Spola',
    console=False,
    icon=str(ROOT / 'build-aux' / 'windows' / 'spola.ico'),
    manifest=str(Path(SPECPATH) / 'spola.manifest'),
)

coll = COLLECT(exe, a.binaries, a.datas, name='Spola')
