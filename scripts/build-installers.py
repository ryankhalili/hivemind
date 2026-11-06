"""Build downloadable connector bundles. Run with PyInstaller installed."""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "downloads"
OUTPUT.mkdir(exist_ok=True)


def archive(folder, filename, files):
    with zipfile.ZipFile(OUTPUT / filename, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in files:
            path = folder / name
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".command") else 0o100644) << 16
            bundle.writestr(info, path.read_bytes())


archive(ROOT / "connectors" / "browser", "HiveMind-Browser.zip", ["manifest.json", "popup.html", "popup.css", "popup.js", "README.md"])
archive(ROOT / "connectors" / "desktop", "HiveMind-Mac.zip", ["HiveMind.command", "app.py", "node.py", "harness.py", "README.md"])
if os.name == "nt":
    build = ROOT / "connectors" / ".build"
    build.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--name", "HiveMind-Setup", "--distpath", str(OUTPUT), "--workpath", str(build / "work"), "--specpath", str(build), str(ROOT / "connectors" / "desktop" / "app.py")], check=True)
    print("Built Windows installer, Mac source launcher, and browser extension in downloads/")
else:
    print("Built Mac source launcher and browser extension. Build the Windows executable on Windows.")
