param(
  [string]$Version = "1.13.1",
  [ValidateSet("x64", "arm64")]
  [string]$Architecture = "x64"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$BuildEnv = Join-Path $ProjectDir ".build-venv-windows-$Architecture"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ProjectDir ".pyinstaller-cache-windows-$Architecture"
$PythonArchitecture = (& $PythonBin -c "import platform; print({'AMD64':'x64','x86_64':'x64','ARM64':'arm64','aarch64':'arm64'}.get(platform.machine(), platform.machine().lower()))").Trim()
if ($PythonArchitecture -ne $Architecture) {
  throw "The $Architecture package must be built by a native $Architecture Python process; found $PythonArchitecture."
}
$NodeBinary = $env:CLIPFARMPILOT_NODE_BINARY
if (-not $NodeBinary) {
  $NodeBinary = (Get-Command node -ErrorAction Stop).Source
}
$NodeArchitecture = (& $NodeBinary -p "process.arch").Trim()
if ($NodeArchitecture -ne $Architecture) {
  throw "The $Architecture package must contain native $Architecture Node.js; found $NodeArchitecture."
}

& $PythonBin -m venv $BuildEnv
$BuildPython = Join-Path $BuildEnv "Scripts\python.exe"
& $BuildPython -m pip install -r backend/requirements.txt -r desktop-requirements.txt
& $BuildPython build_assets/generate_icon.py
& $BuildPython scripts/prepare_caption_runtime.py --platform windows --architecture $Architecture

$ExtraBinaries = @()
if ($Architecture -eq "arm64") {
  & $BuildPython scripts/prepare_windows_arm64_ffmpeg.py
  $ArmRuntime = Join-Path $ProjectDir ".desktop-runtime\windows-arm64"
  $ArmFfmpeg = Join-Path $ArmRuntime "ffmpeg.exe"
  $ArmFfprobe = Join-Path $ArmRuntime "ffprobe.exe"
  if (-not (Test-Path $ArmFfmpeg) -or -not (Test-Path $ArmFfprobe)) {
    throw "The native Windows ARM64 video tools were not prepared."
  }
  $ExtraBinaries = @("--add-binary", "$ArmFfmpeg;bin", "--add-binary", "$ArmFfprobe;bin")
}

$PyInstallerArgs = @(
  "--noconfirm", "--clean", "--windowed", "--onedir",
  "--name", "ClipFarmPilot",
  "--icon", "build_assets/ClipFarmPilot.ico",
  "--add-data", "backend/app/static;backend/app/static",
  "--add-data", "backend/app/assets;backend/app/assets",
  "--add-data", ".caption-runtime;caption_runtime",
  "--add-data", "THIRD_PARTY_NOTICES.md;.",
  "--add-binary", "$NodeBinary;bin",
  "--collect-all", "imageio_ffmpeg",
  "--collect-all", "yt_dlp",
  "--collect-all", "yt_dlp_ejs",
  "--collect-all", "webview",
  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "uvicorn.lifespan.on"
) + $ExtraBinaries + @("desktop_launcher.py")
& $BuildPython -m PyInstaller @PyInstallerArgs

$OutputDir = Join-Path $ProjectDir "outputs"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$Archive = Join-Path $OutputDir "Clip-Farm-Pilot-Windows-v$Version-$Architecture.zip"
Compress-Archive -Path (Join-Path $ProjectDir "dist\ClipFarmPilot") -DestinationPath $Archive -Force
Write-Output "Built Clip Farm Pilot $Version for Windows $Architecture at $Archive"
