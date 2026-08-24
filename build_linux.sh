#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ENV="$PROJECT_DIR/.build-venv-linux"
NODE_BINARY="${CLIPFARMPILOT_NODE_BINARY:-$(command -v node || true)}"
APP_VERSION="${CLIPFARMPILOT_VERSION:-1.6.0}"
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyinstaller-cache-linux"

if [[ -z "$NODE_BINARY" || ! -x "$NODE_BINARY" ]]; then
  echo "Set CLIPFARMPILOT_NODE_BINARY to a Linux x64 Node.js executable before building."
  exit 1
fi

"$PYTHON_BIN" -m venv --system-site-packages "$BUILD_ENV"
"$BUILD_ENV/bin/python" -m pip install -r backend/requirements.txt -r desktop-requirements.txt
"$BUILD_ENV/bin/python" build_assets/generate_icon.py

"$BUILD_ENV/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name ClipFarmPilot \
  --icon build_assets/ClipFarmPilot.iconset/icon_512x512.png \
  --add-data "backend/app/static:backend/app/static" \
  --add-data "backend/app/assets:backend/app/assets" \
  --add-binary "$NODE_BINARY:bin" \
  --collect-all imageio_ffmpeg \
  --collect-all yt_dlp \
  --collect-all yt_dlp_ejs \
  --collect-all webview \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  desktop_launcher.py

mkdir -p outputs
tar -czf "outputs/Clip-Farm-Pilot-Linux-v${APP_VERSION}-x64.tar.gz" -C dist ClipFarmPilot
echo "Built Clip Farm Pilot $APP_VERSION for Linux at outputs/Clip-Farm-Pilot-Linux-v${APP_VERSION}-x64.tar.gz"
