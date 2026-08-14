#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ENV="$PROJECT_DIR/.build-venv"
NODE_BINARY="${CLIPPILOT_NODE_BINARY:-}"
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyinstaller-cache"

if [[ -z "$NODE_BINARY" ]]; then
  NODE_BINARY="$(command -v node 2>/dev/null || true)"
fi

if [[ -z "$NODE_BINARY" || ! -x "$NODE_BINARY" ]]; then
  echo "Set CLIPPILOT_NODE_BINARY to an arm64 Node.js executable before building."
  exit 1
fi

"$PYTHON_BIN" -m venv "$BUILD_ENV"
"$BUILD_ENV/bin/python" -m pip install -r backend/requirements.txt -r desktop-requirements.txt
"$BUILD_ENV/bin/python" build_assets/generate_icon.py

"$BUILD_ENV/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name ClipPilot \
  --icon build_assets/ClipPilot.iconset/icon_512x512@2x.png \
  --osx-bundle-identifier com.clippilot.desktop \
  --target-architecture arm64 \
  --add-data "backend/app/static:backend/app/static" \
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

codesign --force --deep --sign - "$PROJECT_DIR/dist/ClipPilot.app"
echo "Built $PROJECT_DIR/dist/ClipPilot.app"
