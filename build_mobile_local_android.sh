#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ANDROID_DIR="$PROJECT_DIR/mobile-local/android"
OUTPUTS_DIR="$PROJECT_DIR/outputs"
APP_VERSION="${CLIPFARMPILOT_VERSION:-1.9.0}"
TARGET_APK="$OUTPUTS_DIR/Clip-Farm-Pilot-Android-v${APP_VERSION}-Local.apk"

if [[ -z "${JAVA_HOME:-}" && -d /opt/homebrew/opt/openjdk@17 ]]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk@17
fi
if [[ -z "${ANDROID_SDK_ROOT:-}" && -d /opt/homebrew/share/android-commandlinetools ]]; then
  export ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools
fi
export ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-/private/tmp/clipfarmpilot-gradle-cache}"

if [[ -z "${JAVA_HOME:-}" || -z "${ANDROID_HOME:-}" ]]; then
  echo "JAVA_HOME and ANDROID_HOME/ANDROID_SDK_ROOT are required."
  exit 1
fi

mkdir -p "$OUTPUTS_DIR" "$GRADLE_USER_HOME"
if rg -n 'onrender\.com|EXPO_PUBLIC_API_BASE_URL|http://localhost|https://localhost' "$ANDROID_DIR/app/src/main" \
  --glob '*.java' --glob '*.kt'; then
  echo "Privacy check failed: local Android source contains a runtime endpoint."
  exit 1
fi
(
  cd "$ANDROID_DIR"
  ./gradlew :app:assembleRelease --no-daemon
)

SOURCE_APK="$ANDROID_DIR/app/build/outputs/apk/release/app-release.apk"
if [[ ! -s "$SOURCE_APK" ]]; then
  echo "Android release APK was not created."
  exit 1
fi
cp "$SOURCE_APK" "$TARGET_APK"

APKSIGNER="$(find "$ANDROID_HOME/build-tools" -type f -name apksigner 2>/dev/null | sort | tail -n 1)"
AAPT="$(find "$ANDROID_HOME/build-tools" -type f -name aapt 2>/dev/null | sort | tail -n 1)"
if [[ -z "$APKSIGNER" || -z "$AAPT" ]]; then
  echo "Android build-tools (apksigner and aapt) are required for release verification."
  exit 1
fi

"$APKSIGNER" verify --verbose --print-certs "$TARGET_APK"
PERMISSIONS="$("$AAPT" dump permissions "$TARGET_APK")"
if printf '%s\n' "$PERMISSIONS" | grep -q 'android.permission.INTERNET'; then
  echo "Privacy check failed: local APK requests INTERNET permission."
  exit 1
fi
printf '%s\n' "$PERMISSIONS"

DIGEST="$(shasum -a 256 "$TARGET_APK" | awk '{print $1}')"
printf '%s  %s\n' "$DIGEST" "$(basename "$TARGET_APK")" > "$TARGET_APK.sha256"
echo "Built fully local Android APK at $TARGET_APK"
