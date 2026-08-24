#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path


WHISPER_RELEASE = "b4938"
MODEL_NAME = "ggml-base.en.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
MODEL_SHA256 = "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"
SOURCE_URL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{WHISPER_RELEASE}.tar.gz"
SOURCE_SHA256 = "6d8d70a014ca2b10f8a6d006b8f423e5f5ef2afcfbe92b57ab4e01107238112a"
PLATFORM_ARCHIVES = {
    "windows": (
        f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_RELEASE}/whisper-bin-x64.zip",
        "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d",
    ),
    "linux": (
        f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_RELEASE}/whisper-bin-ubuntu-x64.tar.gz",
        "f4cfc1f969a13805908fb72043ce7cc896eb42e0b8afbe841dc8e7298923b061",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and sha256(destination) == expected_sha256:
        return
    temporary = destination.with_suffix(destination.suffix + ".download")
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256(temporary)
    if actual != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Caption runtime checksum mismatch for {url}: {actual}")
    temporary.replace(destination)


def detected_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    raise RuntimeError(f"Unsupported caption runtime platform: {system}")


def prepare_macos(cache: Path, binary_dir: Path) -> None:
    archive = cache / f"whisper-{WHISPER_RELEASE}-source.tar.gz"
    download(SOURCE_URL, archive, SOURCE_SHA256)
    source = cache / f"whisper.cpp-{WHISPER_RELEASE}"
    build = cache / f"whisper.cpp-{WHISPER_RELEASE}-build-arm64"
    if not source.is_dir():
        source.mkdir(parents=True)
        with tarfile.open(archive, "r:gz") as package:
            members = package.getmembers()
            prefix = members[0].name.split("/", 1)[0] + "/"
            for member in members:
                if not member.name.startswith(prefix) or member.name == prefix:
                    continue
                member.name = member.name[len(prefix):]
                package.extract(member, source, filter="data")
    executable = build / "bin" / "whisper-cli"
    if not executable.is_file():
        subprocess.run(
            [
                "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_OSX_ARCHITECTURES=arm64",
                "-DCMAKE_SYSTEM_PROCESSOR=arm64", "-DGGML_NATIVE=OFF",
                "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF",
            ],
            check=True,
        )
        subprocess.run(["cmake", "--build", str(build), "--target", "whisper-cli", "-j", "6"], check=True)
    shutil.copytree(build / "bin", binary_dir, dirs_exist_ok=True, symlinks=True)


def prepare_archive_runtime(target_platform: str, cache: Path, binary_dir: Path) -> None:
    url, expected_sha256 = PLATFORM_ARCHIVES[target_platform]
    archive = cache / Path(url).name
    download(url, archive, expected_sha256)
    with tempfile.TemporaryDirectory(prefix="clipfarmpilot-whisper-") as temporary_name:
        temporary = Path(temporary_name)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as package:
                package.extractall(temporary)
            source = temporary / "Release"
        else:
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(temporary, filter="data")
            source = temporary / "whisper-bin-ubuntu-x64"
        shutil.copytree(source, binary_dir, dirs_exist_ok=True, symlinks=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Clip Farm Pilot's offline live-caption runtime.")
    parser.add_argument("--platform", choices=("macos", "windows", "linux"), default=detected_platform())
    parser.add_argument("--output", type=Path, default=Path(".caption-runtime"))
    parser.add_argument("--cache", type=Path, default=Path(".caption-runtime-cache"))
    args = parser.parse_args()

    output = args.output.resolve()
    cache = args.cache.resolve()
    binary_dir = output / "bin"
    model_dir = output / "models"
    # Runtime archives contain versioned-library symlinks. Recreate the binary
    # tree so repeated builds never collide with stale links or another target.
    shutil.rmtree(binary_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    if args.platform == "macos":
        prepare_macos(cache, binary_dir)
    else:
        prepare_archive_runtime(args.platform, cache, binary_dir)
    model_path = model_dir / MODEL_NAME
    download(MODEL_URL, model_path, MODEL_SHA256)
    executable = binary_dir / ("whisper-cli.exe" if args.platform == "windows" else "whisper-cli")
    executable.chmod(executable.stat().st_mode | 0o111)
    (output / "runtime.json").write_text(
        json.dumps({"engine": "whisper.cpp", "release": WHISPER_RELEASE, "model": MODEL_NAME}) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared offline live captions at {output}")


if __name__ == "__main__":
    main()
