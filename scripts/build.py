"""Build a standalone app for the current OS with PyInstaller and package it for download.

    macOS   -> dist/MusicTagChecker-macOS-AppleSilicon.dmg  (or -Intel.dmg on Intel Macs)
    Windows -> dist/MusicTagChecker-Windows.exe

Usage:  python scripts/build.py      (needs: pip install -r requirements.txt pyinstaller)
"""
import os
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "Music Tag Checker"
BUNDLE_ID = "com.corduroy420.musictagchecker"
sys.path.insert(0, ROOT)
from version import __version__  # noqa: E402


def run(*args):
    print("+", " ".join(args), flush=True)
    subprocess.check_call(args, cwd=ROOT)


def main():
    run(sys.executable, "scripts/make_icon.py")
    system = platform.system()
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", APP_NAME, "music_tag_checker.py"]
    if system == "Darwin":
        args += ["--icon", "build_assets/icon.icns", "--osx-bundle-identifier", BUNDLE_ID]
    elif system == "Windows":
        args += ["--onefile", "--icon", "build_assets/icon.ico"]
    run(*args)

    dist = os.path.join(ROOT, "dist")
    if system == "Darwin":
        app = os.path.join(dist, f"{APP_NAME}.app")
        info_path = os.path.join(app, "Contents", "Info.plist")
        with open(info_path, "rb") as fh:
            info = plistlib.load(fh)
        info.update(CFBundleShortVersionString=__version__, CFBundleVersion=__version__,
                    NSHighResolutionCapable=True, NSRequiresAquaSystemAppearance=False)
        with open(info_path, "wb") as fh:
            plistlib.dump(info, fh)
        run("codesign", "--force", "--deep", "--sign", "-", app)  # re-sign (ad hoc) after editing Info.plist
        arch = "AppleSilicon" if platform.machine() == "arm64" else "Intel"
        output = os.path.join(dist, f"MusicTagChecker-macOS-{arch}.dmg")
        staging = os.path.join(dist, "dmg")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        run("ditto", app, os.path.join(staging, f"{APP_NAME}.app"))
        os.symlink("/Applications", os.path.join(staging, "Applications"))  # drag-to-install shortcut
        if os.path.exists(output):
            os.remove(output)
        run("hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging, "-ov", "-format", "UDZO", output)
        shutil.rmtree(staging)
    elif system == "Windows":
        output = os.path.join(dist, "MusicTagChecker-Windows.exe")
        shutil.copyfile(os.path.join(dist, f"{APP_NAME}.exe"), output)
    else:
        sys.exit(f"Packaging for {system} is not set up; the PyInstaller output is in {dist}")
    print(f"\nBuilt {output}")


if __name__ == "__main__":
    main()
