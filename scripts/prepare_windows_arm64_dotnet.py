#!/usr/bin/env python3
"""Bundle Microsoft's .NET desktop runtime for the Windows ARM64 package.

pywebview draws its Windows window with WinForms through pythonnet. On x64,
pythonnet uses the .NET Framework built into Windows, but clr_loader ships no
ARM64 build of its .NET Framework loader. On ARM64 pywebview falls back to
modern .NET, which only has WinForms when the Windows Desktop runtime is
present. Shipping that runtime inside the app means the window opens without
asking people to install anything.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


DOTNET_VERSION = "10.0.12"
ARCHIVES = (
    (
        f"https://builds.dotnet.microsoft.com/dotnet/Runtime/{DOTNET_VERSION}/"
        f"dotnet-runtime-{DOTNET_VERSION}-win-arm64.zip",
        "54039abe0d12e18fd746d441bbeeb2ba53e0df882a89cb8a9915472b8a5cb740",
    ),
    (
        f"https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/{DOTNET_VERSION}/"
        f"windowsdesktop-runtime-{DOTNET_VERSION}-win-arm64.zip",
        "df535b029518e4a589edf32d672cecfa2a0c0c3f6406da1e0c9ce7c1dd9d1f57",
    ),
)
RUNTIME_CONFIG_NAME = "clipfarmpilot.runtimeconfig.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and sha256(destination) == expected_sha256:
        return
    with tempfile.NamedTemporaryFile(prefix="clipfarmpilot-dotnet-", suffix=".zip", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with urllib.request.urlopen(url, timeout=180) as response, temporary_path.open("wb") as target:
            shutil.copyfileobj(response, target)
        actual = sha256(temporary_path)
        if actual != expected_sha256:
            raise RuntimeError(f".NET runtime checksum mismatch for {url}: {actual}")
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    runtime_cache = project / ".desktop-runtime"
    dotnet_root = runtime_cache / "windows-arm64-dotnet"
    shutil.rmtree(dotnet_root, ignore_errors=True)
    dotnet_root.mkdir(parents=True, exist_ok=True)

    for url, expected_sha256 in ARCHIVES:
        archive = runtime_cache / Path(url).name
        download(url, archive, expected_sha256)
        with zipfile.ZipFile(archive) as package:
            package.extractall(dotnet_root)

    required = (
        dotnet_root / "host" / "fxr" / DOTNET_VERSION / "hostfxr.dll",
        dotnet_root / "shared" / "Microsoft.NETCore.App" / DOTNET_VERSION / "System.Private.CoreLib.dll",
        dotnet_root / "shared" / "Microsoft.WindowsDesktop.App" / DOTNET_VERSION / "System.Windows.Forms.dll",
    )
    missing = [str(path.relative_to(dotnet_root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"The bundled .NET desktop runtime is incomplete: {', '.join(missing)}")

    # Asking for the desktop framework also brings in the base runtime it builds on.
    (dotnet_root / RUNTIME_CONFIG_NAME).write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "tfm": "net10.0",
                    "framework": {"name": "Microsoft.WindowsDesktop.App", "version": DOTNET_VERSION},
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Prepared the .NET {DOTNET_VERSION} desktop runtime for Windows ARM64 at {dotnet_root}")


if __name__ == "__main__":
    main()
