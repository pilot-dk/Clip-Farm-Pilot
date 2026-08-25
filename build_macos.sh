#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ENV="$PROJECT_DIR/.build-venv"
NODE_BINARY="${CLIPFARMPILOT_NODE_BINARY:-}"
APP_NAME="Clip Farm Pilot"
APP_VERSION="${CLIPFARMPILOT_VERSION:-1.9.0}"
BUILD_NUMBER="${CLIPFARMPILOT_BUILD_NUMBER:-${APP_VERSION//./}}"
APP_BUNDLE="$PROJECT_DIR/dist/$APP_NAME.app"
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyinstaller-cache"

if [[ -z "$NODE_BINARY" ]]; then
  NODE_BINARY="$(command -v node 2>/dev/null || true)"
fi

if [[ -z "$NODE_BINARY" || ! -x "$NODE_BINARY" ]]; then
  echo "Set CLIPFARMPILOT_NODE_BINARY to an arm64 Node.js executable before building."
  exit 1
fi

"$PYTHON_BIN" -m venv "$BUILD_ENV"
"$BUILD_ENV/bin/python" -m pip install -r backend/requirements.txt -r desktop-requirements.txt
"$BUILD_ENV/bin/python" build_assets/generate_icon.py
"$BUILD_ENV/bin/python" scripts/prepare_caption_runtime.py --platform macos

"$BUILD_ENV/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name "$APP_NAME" \
  --icon build_assets/ClipFarmPilot.iconset/icon_512x512@2x.png \
  --osx-bundle-identifier com.clipfarmpilot.desktop \
  --target-architecture arm64 \
  --add-data "backend/app/static:backend/app/static" \
  --add-data "backend/app/assets:backend/app/assets" \
  --add-data ".caption-runtime:caption_runtime" \
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

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APP_VERSION" "$APP_BUNDLE/Contents/Info.plist"
if ! /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$APP_BUNDLE/Contents/Info.plist"; then
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $BUILD_NUMBER" "$APP_BUNDLE/Contents/Info.plist"
fi
codesign --force --deep --sign - "$APP_BUNDLE"
echo "Built $APP_NAME $APP_VERSION at $APP_BUNDLE"
