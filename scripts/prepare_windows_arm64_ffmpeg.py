#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ARCHIVE_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "autobuild-2026-09-03-13-17/ffmpeg-N-126390-g9fc8c785e2-winarm64-gpl.zip"
)
ARCHIVE_SHA256 = "b450c50c4522f4a50304b3b09ea4424c1a9c2a8b1ba724c9570db4e1cf56d571"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    output = project / ".desktop-runtime" / "windows-arm64"
    output.mkdir(parents=True, exist_ok=True)
    archive = project / ".desktop-runtime" / Path(ARCHIVE_URL).name
    if not archive.is_file() or sha256(archive) != ARCHIVE_SHA256:
        with tempfile.NamedTemporaryFile(
            prefix="clipfarmpilot-ffmpeg-", suffix=".zip", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with urllib.request.urlopen(ARCHIVE_URL, timeout=180) as response, temporary_path.open("wb") as target:
                shutil.copyfileobj(response, target)
            actual = sha256(temporary_path)
            if actual != ARCHIVE_SHA256:
                raise RuntimeError(f"Windows ARM64 FFmpeg checksum mismatch: {actual}")
            temporary_path.replace(archive)
        finally:
            temporary_path.unlink(missing_ok=True)

    with zipfile.ZipFile(archive) as package:
        members = {Path(name).name.lower(): name for name in package.namelist() if "/bin/" in name.replace("\\", "/")}
        for executable in ("ffmpeg.exe", "ffprobe.exe"):
            member = members.get(executable)
            if not member:
                raise RuntimeError(f"The pinned Windows ARM64 archive is missing {executable}.")
            with package.open(member) as source, (output / executable).open("wb") as target:
                shutil.copyfileobj(source, target)
    print(f"Prepared native Windows ARM64 video tools at {output}")


if __name__ == "__main__":
    main()
