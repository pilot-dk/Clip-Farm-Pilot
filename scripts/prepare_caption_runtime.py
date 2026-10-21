#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
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
# Silero voice-activity model: finds the pauses in speech even over game audio and music.
VAD_MODEL_NAME = "ggml-silero-v6.2.0.bin"
VAD_MODEL_URL = f"https://huggingface.co/ggml-org/whisper-vad/resolve/main/{VAD_MODEL_NAME}"
VAD_MODEL_SHA256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
TOOLS = ("whisper-cli", "whisper-vad-speech-segments")
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

# llama.cpp's llama-server runs the auto-caption model. Its own release builds
# are used on every platform, pinned to one build and checked by checksum.
LLAMA_RELEASE = "b11799"
LLAMA_ARCHIVES = {
    ("macos", "arm64"): ("llama-b11799-bin-macos-arm64.tar.gz", "51c3471c9212b41e51a6696c0b966628fa45d625385fdb024c4483e8d3154dca"),
    ("linux", "x64"): ("llama-b11799-bin-ubuntu-x64.tar.gz", "4920c65448350c28f8ce7b35eec91566807e0f111c25a0df2d5fc12a1ed1716a"),
    ("linux", "arm64"): ("llama-b11799-bin-ubuntu-arm64.tar.gz", "d53caf55e76e56f1718e08ab7c3c3fb9746a7c817c81f6a3fc5e2f15e601d82b"),
    ("windows", "x64"): ("llama-b11799-bin-win-cpu-x64.zip", "e77aee2c06504a5821d2127b98e71c9228507292ee1cb72b120280bdcb616291"),
    ("windows", "arm64"): ("llama-b11799-bin-win-cpu-arm64.zip", "5efeb456bbade9d777b178afefdb059bde75ade9254f64e7679a4869dff518a0"),
}
# The Windows engines are built against the Visual C++ runtime, which not every
# PC has installed, so its three DLLs travel with them.
WINDOWS_RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


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


def detected_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"amd64", "x86_64"}:
        return "x64"
    raise RuntimeError(f"Unsupported caption runtime architecture: {machine}")


def source_tree(cache: Path) -> Path:
    archive = cache / f"whisper-{WHISPER_RELEASE}-source.tar.gz"
    download(SOURCE_URL, archive, SOURCE_SHA256)
    source = cache / f"whisper.cpp-{WHISPER_RELEASE}"
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
    return source


def prepare_macos(cache: Path, binary_dir: Path, architecture: str) -> None:
    source = source_tree(cache)
    cmake_architecture = "arm64" if architecture == "arm64" else "x86_64"
    build = cache / f"whisper.cpp-{WHISPER_RELEASE}-build-macos-{architecture}"
    if not all((build / "bin" / tool).is_file() for tool in TOOLS):
        subprocess.run(
            [
                "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_OSX_ARCHITECTURES={cmake_architecture}",
                f"-DCMAKE_SYSTEM_PROCESSOR={cmake_architecture}", "-DGGML_NATIVE=OFF",
                "-DBUILD_SHARED_LIBS=ON",
                "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF",
            ],
            check=True,
        )
        subprocess.run(["cmake", "--build", str(build), "--target", *TOOLS, "-j", "6"], check=True)
    shutil.copytree(build / "bin", binary_dir, dirs_exist_ok=True, symlinks=True)


def prepare_source_runtime(target_platform: str, architecture: str, cache: Path, binary_dir: Path) -> None:
    """Build native whisper.cpp tools when an upstream archive is unavailable."""
    source = source_tree(cache)
    build = cache / f"whisper.cpp-{WHISPER_RELEASE}-build-{target_platform}-{architecture}"
    if target_platform == "windows":
        # whisper.cpp refuses to compile its ARM kernels with MSVC. Visual Studio's
        # ClangCL toolset builds them with clang-cl, and GGML_NATIVE=OFF keeps the
        # generic ARMv8 baseline so the tool runs on every Windows on Arm laptop.
        build = build.with_name(f"{build.name}-clangcl")
        executables = [build / "bin" / "Release" / f"{tool}.exe" for tool in TOOLS]
        configure = [
            "cmake", "-S", str(source), "-B", str(build), "-A", "ARM64", "-T", "ClangCL",
            "-DCMAKE_BUILD_TYPE=Release", "-DGGML_NATIVE=OFF", "-DBUILD_SHARED_LIBS=ON",
            "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF",
        ]
        build_command = [
            "cmake", "--build", str(build), "--config", "Release",
            "--target", *TOOLS, "--parallel", "4",
        ]
        output_dir = build / "bin" / "Release"
    else:
        executables = [build / "bin" / tool for tool in TOOLS]
        configure = [
            "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", "-DGGML_NATIVE=OFF", "-DBUILD_SHARED_LIBS=ON",
            "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF",
        ]
        build_command = ["cmake", "--build", str(build), "--target", *TOOLS, "-j", "4"]
        output_dir = build / "bin"
    if not all(executable.is_file() for executable in executables):
        subprocess.run(configure, check=True)
        subprocess.run(build_command, check=True)
    shutil.copytree(output_dir, binary_dir, dirs_exist_ok=True, symlinks=True)


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


def keep_llama_file(name: str) -> bool:
    """Whether a file from a llama.cpp release is needed to run llama-server.

    The release carries every llama.cpp tool. Only the server, the shared
    libraries it loads and the licences are kept; each other tool's own
    "-impl" library is left out.
    """
    lowered = name.lower()
    if lowered in {"llama-server", "llama-server.exe"} or lowered.startswith("license"):
        return True
    library = lowered.endswith((".dylib", ".dll")) or ".so" in lowered
    return library and ("-impl." not in lowered or "server-impl." in lowered)


def prepare_llama_runtime(target_platform: str, architecture: str, cache: Path, llm_dir: Path) -> None:
    name, expected_sha256 = LLAMA_ARCHIVES[(target_platform, architecture)]
    archive = cache / name
    download(f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/{name}", archive, expected_sha256)
    with tempfile.TemporaryDirectory(prefix="clipfarmpilot-llama-") as temporary_name:
        temporary = Path(temporary_name)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as package:
                package.extractall(temporary)
        else:
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(temporary, filter="data")
        server = next(temporary.rglob("llama-server.exe" if target_platform == "windows" else "llama-server"))
        for item in server.parent.iterdir():
            if item.is_file() and keep_llama_file(item.name):
                shutil.copy2(item, llm_dir / item.name, follow_symlinks=False)
    executable = llm_dir / ("llama-server.exe" if target_platform == "windows" else "llama-server")
    executable.chmod(executable.stat().st_mode | 0o111)


def windows_runtime_dir(architecture: str) -> Path:
    """The Visual C++ runtime DLLs for the target, from Visual Studio's redistributable folder."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    installations: list[str] = []
    if vswhere.is_file():
        result = subprocess.run(
            [str(vswhere), "-all", "-products", "*", "-property", "installationPath"],
            capture_output=True, text=True, check=False,
        )
        installations = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for installation in installations:
        pattern = os.path.join(installation, "VC", "Redist", "MSVC", "*", architecture, "Microsoft.VC14*.CRT")
        for folder in sorted(glob.glob(pattern), reverse=True):
            if all((Path(folder) / dll).is_file() for dll in WINDOWS_RUNTIME_DLLS):
                return Path(folder)
    # The build machine runs the target architecture, so its own copies match.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    if all((system / dll).is_file() for dll in WINDOWS_RUNTIME_DLLS):
        return system
    raise RuntimeError("The Visual C++ runtime DLLs for the caption engines were not found.")


def bundle_linux_openmp(*destinations: Path) -> None:
    """Ship libgomp beside the engines: they are built with OpenMP, and a minimal system may lack it."""
    result = subprocess.run(["gcc", "-print-file-name=libgomp.so.1"], capture_output=True, text=True, check=False)
    found = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    if found is None or not found.is_absolute() or not found.exists():
        print("libgomp.so.1 was not found; the caption engines will use the system's copy.")
        return
    for destination in destinations:
        shutil.copy2(found.resolve(), destination / "libgomp.so.1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Clip Farm Pilot's offline live-caption runtime.")
    parser.add_argument("--platform", choices=("macos", "windows", "linux"), default=detected_platform())
    parser.add_argument("--architecture", choices=("x64", "arm64"), default=detected_architecture())
    parser.add_argument("--output", type=Path, default=Path(".caption-runtime"))
    parser.add_argument("--cache", type=Path, default=Path(".caption-runtime-cache"))
    parser.add_argument("--no-llm", action="store_true", help="Leave out the auto-caption engine (web deployments).")
    args = parser.parse_args()

    output = args.output.resolve()
    cache = args.cache.resolve()
    binary_dir = output / "bin"
    llm_dir = output / "llm"
    model_dir = output / "models"
    # Runtime archives contain versioned-library symlinks. Recreate the binary
    # trees so repeated builds never collide with stale links or another target.
    shutil.rmtree(binary_dir, ignore_errors=True)
    shutil.rmtree(llm_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    if args.platform == "macos":
        prepare_macos(cache, binary_dir, args.architecture)
    elif args.architecture == "arm64":
        prepare_source_runtime(args.platform, args.architecture, cache, binary_dir)
    else:
        prepare_archive_runtime(args.platform, cache, binary_dir)
    engine_dirs = [binary_dir]
    if not args.no_llm:
        # A folder of its own: llama.cpp ships ggml libraries with the same
        # names as Whisper's but a different version.
        llm_dir.mkdir(parents=True, exist_ok=True)
        prepare_llama_runtime(args.platform, args.architecture, cache, llm_dir)
        engine_dirs.append(llm_dir)
    if args.platform == "windows":
        runtime = windows_runtime_dir(args.architecture)
        for engine_dir in engine_dirs:
            for dll in WINDOWS_RUNTIME_DLLS:
                shutil.copy2(runtime / dll, engine_dir / dll)
    elif args.platform == "linux":
        bundle_linux_openmp(*engine_dirs)
    model_path = model_dir / MODEL_NAME
    download(MODEL_URL, model_path, MODEL_SHA256)
    download(VAD_MODEL_URL, model_dir / VAD_MODEL_NAME, VAD_MODEL_SHA256)
    suffix = ".exe" if args.platform == "windows" else ""
    for tool in TOOLS:
        executable = binary_dir / f"{tool}{suffix}"
        if not executable.is_file():
            raise RuntimeError(f"The prepared caption runtime is missing {executable.name}.")
        executable.chmod(executable.stat().st_mode | 0o111)
    (output / "runtime.json").write_text(
        json.dumps({
            "engine": "whisper.cpp",
            "release": WHISPER_RELEASE,
            "model": MODEL_NAME,
            "vad_model": VAD_MODEL_NAME,
            "architecture": args.architecture,
            "llama_release": None if args.no_llm else LLAMA_RELEASE,
        }) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared offline live captions at {output}")


if __name__ == "__main__":
    main()
