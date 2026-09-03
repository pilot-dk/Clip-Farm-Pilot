#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
MACHINE_ARCH="${CLIPFARMPILOT_ARCHITECTURE:-$(uname -m)}"
case "$MACHINE_ARCH" in
  arm64|aarch64) APP_ARCH="arm64" ;;
  x64|x86_64|amd64) APP_ARCH="x64" ;;
  *) echo "Unsupported Linux architecture: $MACHINE_ARCH"; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) HOST_ARCH="arm64" ;;
  x64|x86_64|amd64) HOST_ARCH="x64" ;;
  *) echo "Unsupported Linux host architecture: $(uname -m)"; exit 1 ;;
esac
if [[ "$HOST_ARCH" != "$APP_ARCH" ]]; then
  echo "The $APP_ARCH package must be built on a native $APP_ARCH Linux runner; found $HOST_ARCH."
  exit 1
fi
BUILD_ENV="$PROJECT_DIR/.build-venv-linux-$APP_ARCH"
NODE_BINARY="${CLIPFARMPILOT_NODE_BINARY:-$(command -v node || true)}"
APP_VERSION="${CLIPFARMPILOT_VERSION:-1.13.1}"
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyinstaller-cache-linux-$APP_ARCH"

if [[ -z "$NODE_BINARY" || ! -x "$NODE_BINARY" ]]; then
  echo "Set CLIPFARMPILOT_NODE_BINARY to a Linux $APP_ARCH Node.js executable before building."
  exit 1
fi
PYTHON_ARCH="$($PYTHON_BIN -c 'import platform; print({"AMD64":"x64","x86_64":"x64","ARM64":"arm64","aarch64":"arm64"}.get(platform.machine(), platform.machine().lower()))')"
if [[ "$PYTHON_ARCH" != "$APP_ARCH" ]]; then
  echo "The $APP_ARCH package must be built by native $APP_ARCH Python; found $PYTHON_ARCH."
  exit 1
fi
NODE_ARCH="$($NODE_BINARY -p 'process.arch')"
if [[ "$NODE_ARCH" != "$APP_ARCH" ]]; then
  echo "The $APP_ARCH package must contain native $APP_ARCH Node.js; found $NODE_ARCH."
  exit 1
fi

"$PYTHON_BIN" -m venv --system-site-packages "$BUILD_ENV"
"$BUILD_ENV/bin/python" -m pip install -r backend/requirements.txt -r desktop-requirements.txt
"$BUILD_ENV/bin/python" build_assets/generate_icon.py
"$BUILD_ENV/bin/python" scripts/prepare_caption_runtime.py --platform linux --architecture "$APP_ARCH"

"$BUILD_ENV/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name ClipFarmPilot \
  --icon build_assets/ClipFarmPilot.iconset/icon_512x512.png \
  --add-data "backend/app/static:backend/app/static" \
  --add-data "backend/app/assets:backend/app/assets" \
  --add-data ".caption-runtime:caption_runtime" \
  --add-data "THIRD_PARTY_NOTICES.md:." \
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
tar -czf "outputs/Clip-Farm-Pilot-Linux-v${APP_VERSION}-${APP_ARCH}.tar.gz" -C dist ClipFarmPilot
echo "Built Clip Farm Pilot $APP_VERSION for Linux $APP_ARCH at outputs/Clip-Farm-Pilot-Linux-v${APP_VERSION}-${APP_ARCH}.tar.gz"
