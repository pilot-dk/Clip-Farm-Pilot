param(
  [string]$Version = "1.11.0"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$BuildEnv = Join-Path $ProjectDir ".build-venv-windows"
$NodeBinary = $env:CLIPFARMPILOT_NODE_BINARY
if (-not $NodeBinary) {
  $NodeBinary = (Get-Command node -ErrorAction Stop).Source
}

& $PythonBin -m venv $BuildEnv
$BuildPython = Join-Path $BuildEnv "Scripts\python.exe"
& $BuildPython -m pip install -r backend/requirements.txt -r desktop-requirements.txt
& $BuildPython build_assets/generate_icon.py
& $BuildPython scripts/prepare_caption_runtime.py --platform windows

& $BuildPython -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --name ClipFarmPilot `
  --icon build_assets/ClipFarmPilot.ico `
  --add-data "backend/app/static;backend/app/static" `
  --add-data "backend/app/assets;backend/app/assets" `
  --add-data ".caption-runtime;caption_runtime" `
  --add-binary "$NodeBinary;bin" `
  --collect-all imageio_ffmpeg `
  --collect-all yt_dlp `
  --collect-all yt_dlp_ejs `
  --collect-all webview `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  desktop_launcher.py

$OutputDir = Join-Path $ProjectDir "outputs"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$Archive = Join-Path $OutputDir "Clip-Farm-Pilot-Windows-v$Version-x64.zip"
Compress-Archive -Path (Join-Path $ProjectDir "dist\ClipFarmPilot") -DestinationPath $Archive -Force
Write-Output "Built Clip Farm Pilot $Version for Windows at $Archive"
