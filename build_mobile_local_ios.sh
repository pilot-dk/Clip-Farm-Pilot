#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
SOURCE_DIR="$PROJECT_DIR/mobile-local/ios/ClipFarmPilotLocal"
OUTPUTS_DIR="$PROJECT_DIR/outputs"
BUILD_ROOT="$(mktemp -d /private/tmp/clipfarmpilot-ios-build.XXXXXX)"
APP_VERSION="${CLIPFARMPILOT_VERSION:-1.9.0}"
APP_DIR="$BUILD_ROOT/Payload/Clip Farm Pilot.app"
EXECUTABLE="$APP_DIR/ClipFarmPilotLocal"
TARGET_IPA="$OUTPUTS_DIR/Clip-Farm-Pilot-iOS-v${APP_VERSION}-Local-Unsigned.ipa"
MODULE_CACHE="/private/tmp/clipfarmpilot-ios-module-cache"

mkdir -p "$OUTPUTS_DIR" "$APP_DIR" "$MODULE_CACHE"

if rg -n 'URLSession|onrender\.com|EXPO_PUBLIC_API_BASE_URL|http://localhost|https://localhost' "$SOURCE_DIR" --glob '*.swift'; then
  print "Privacy check failed: local iOS source contains a network client or runtime endpoint."
  exit 1
fi

SDK_PATH="$(xcrun --sdk iphoneos --show-sdk-path)"
xcrun --sdk iphoneos swiftc \
  -target arm64-apple-ios17.0 \
  -parse-as-library \
  -O \
  -whole-module-optimization \
  -sdk "$SDK_PATH" \
  -module-cache-path "$MODULE_CACHE" \
  "$SOURCE_DIR/ClipFarmPilotLocalApp.swift" \
  "$SOURCE_DIR/Models.swift" \
  "$SOURCE_DIR/EditorViewModel.swift" \
  "$SOURCE_DIR/LocalVideoEngine.swift" \
  "$SOURCE_DIR/ContentView.swift" \
  -o "$EXECUTABLE"

cp "$SOURCE_DIR/Info.plist" "$APP_DIR/Info.plist"
cp "$SOURCE_DIR/PrivacyInfo.xcprivacy" "$APP_DIR/PrivacyInfo.xcprivacy"
cp "$SOURCE_DIR/Vine boom sound effect.mp3" "$APP_DIR/Vine boom sound effect.mp3"

/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable ClipFarmPilotLocal" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.clipfarmpilot.local" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Clip Farm Pilot" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APP_VERSION" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 190" "$APP_DIR/Info.plist"

ICON_SOURCE="$SOURCE_DIR/Assets.xcassets/AppIcon.appiconset/AppIcon.png"
sips -z 120 120 "$ICON_SOURCE" --out "$APP_DIR/AppIcon60x60@2x.png" >/dev/null
sips -z 180 180 "$ICON_SOURCE" --out "$APP_DIR/AppIcon60x60@3x.png" >/dev/null
sips -z 152 152 "$ICON_SOURCE" --out "$APP_DIR/AppIcon76x76@2x~ipad.png" >/dev/null
sips -z 167 167 "$ICON_SOURCE" --out "$APP_DIR/AppIcon83.5x83.5@2x~ipad.png" >/dev/null

print -n 'APPL????' > "$APP_DIR/PkgInfo"
chmod 755 "$EXECUTABLE"
plutil -lint "$APP_DIR/Info.plist" "$APP_DIR/PrivacyInfo.xcprivacy"
lipo -info "$EXECUTABLE" | grep -q 'arm64'

PACKAGE_IPA="$BUILD_ROOT/Clip-Farm-Pilot.ipa"
(
  cd "$BUILD_ROOT"
  COPYFILE_DISABLE=1 /usr/bin/zip -qry "$PACKAGE_IPA" Payload
)
mv "$PACKAGE_IPA" "$TARGET_IPA"
unzip -tq "$TARGET_IPA"

DIGEST="$(shasum -a 256 "$TARGET_IPA" | awk '{print $1}')"
print "$DIGEST  ${TARGET_IPA:t}" > "$TARGET_IPA.sha256"
print "Built fully local unsigned iPhone/iPad IPA at $TARGET_IPA"
