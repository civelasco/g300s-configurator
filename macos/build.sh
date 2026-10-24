#!/bin/bash
# Builds and installs G300s Configurator.app. Run from anywhere:  macos/build.sh [--force]
#  - src/g300.py, g300_gui.py, g300_gui.html go to ~/Library/Application Support/G300s Configurator
#    (re-run after changing them; the app itself is left alone).
#  - The .app (macos/*.swift, icon) is only rebuilt when the Swift code changes or with --force,
#    because every rebuild gives it a new signature and macOS then asks for Input Monitoring again.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

if ! /usr/bin/python3 -c "import hid" 2>/dev/null; then
  echo "Installing the USB library (hidapi) for the Mac's Python…"
  /usr/bin/python3 -m pip install --user -q -r requirements.txt
fi

NAME="G300s Configurator"
BUILD="build"
APP="$BUILD/$NAME.app"
SUPPORT="$HOME/Library/Application Support/$NAME"

mkdir -p "$SUPPORT"
cp src/g300.py src/g300_gui.py src/g300_gui.html "$SUPPORT/"
echo "Updated app files in: $SUPPORT"

# ~/Applications (still shows in Launchpad and Spotlight). Note: macOS caches apps' privacy
# info by location, so keep installing to the same place to keep the Input Monitoring permission.
DEST="$HOME/Applications"
mkdir -p "$DEST"
STAMP="$(cat macos/*.swift | shasum | cut -d' ' -f1)"
if [ "${1:-}" != "--force" ] && [ -d "$DEST/$NAME.app" ] && [ "$(cat "$BUILD/.swift-hash" 2>/dev/null)" = "$STAMP" ]; then
  echo "App window unchanged - keeping $DEST/$NAME.app (and its permissions)."
  exit 0
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "Compiling…"
swiftc -O -o "$APP/Contents/MacOS/G300s" macos/main.swift -framework Cocoa -framework WebKit

echo "Drawing icon…"
rm -rf "$BUILD/AppIcon.iconset"
swift macos/make_icon.swift "$BUILD/AppIcon.iconset" >/dev/null
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"


cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>G300s Configurator</string>
  <key>CFBundleDisplayName</key><string>G300s Configurator</string>
  <key>CFBundleIdentifier</key><string>local.g300s.mouse-configurator</string>
  <key>CFBundleExecutable</key><string>G300s</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Configures Logitech G300 / G300s mice.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# Ad-hoc signature so macOS can remember the Input Monitoring permission.
codesign --force --deep --sign - "$APP"

rm -rf "$DEST/$NAME.app"
cp -R "$APP" "$DEST/"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/$NAME.app"
echo "$STAMP" > "$BUILD/.swift-hash"
echo "Installed: $DEST/$NAME.app  (macOS will ask for Input Monitoring again)"
