#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-run}"
cd "$ROOT_DIR"
mkdir -p "$ROOT_DIR/dist"
xcrun clang -mmacosx-version-min=14.0 -fobjc-arc -O "$ROOT_DIR/script/CursorImageProbe.m" -framework AppKit -o "$ROOT_DIR/dist/cursor-image-probe.next"
mv "$ROOT_DIR/dist/cursor-image-probe.next" "$ROOT_DIR/dist/cursor-image-probe"
if [ "$MODE" = "--cursor-only" ]; then exit 0; fi
xcrun swiftc -target arm64-apple-macos14.0 -O "$ROOT_DIR/script/OfficeCursorRelay.swift" "$ROOT_DIR/script/OfficeCursorRelayMain.swift" "$ROOT_DIR/Sources/StatusFileObserver.swift" -o "$ROOT_DIR/dist/office-cursor-relay"
if [ "$MODE" = "--office-relay-only" ]; then exit 0; fi
xcrun swiftc -target arm64-apple-macos14.0 -O -module-name UUDisplayAutomation "$ROOT_DIR/Sources/main.swift" "$ROOT_DIR/Sources/SessionEvidence.swift" "$ROOT_DIR/Sources/CursorSizeMenu.swift" "$ROOT_DIR/Sources/LoginAtStartup.swift" "$ROOT_DIR/Sources/StorageHealth.swift" "$ROOT_DIR/Sources/ExitHandoff.swift" "$ROOT_DIR/Sources/StatusBarPresentation.swift" "$ROOT_DIR/Sources/RuntimeStatusSnapshot.swift" "$ROOT_DIR/Sources/StatusFileObserver.swift" "$ROOT_DIR/Sources/StatusCopy.swift" "$ROOT_DIR/Sources/DesignSystem.swift" "$ROOT_DIR/Sources/WorkbenchModels.swift" "$ROOT_DIR/Sources/WorkbenchWindowLayout.swift" "$ROOT_DIR/Sources/DeviceWorkbench.swift" "$ROOT_DIR/Sources/StatusPopover.swift" "$ROOT_DIR/Sources/PermissionHandoff.swift" "$ROOT_DIR/Sources/PermissionProcessIdentity.swift" "$ROOT_DIR/Sources/AccessibilityPermission.swift" "$ROOT_DIR/Sources/PermissionGuide.swift" "$ROOT_DIR/Sources/OfficeCursorHost.swift" "$ROOT_DIR/Sources/OfficeCursorInstaller.swift" "$ROOT_DIR/Sources/OfficeCursorClientStatus.swift" "$ROOT_DIR/Sources/OfficeCursorSetup.swift" "$ROOT_DIR/Sources/OfficeCursorSetupView.swift" -o "$ROOT_DIR/dist/UUDisplayAutomation" -framework AppKit -framework ApplicationServices -framework Carbon
xcrun swiftc -target arm64-apple-macos14.0 -O "$ROOT_DIR/script/Topology.swift" -o "$ROOT_DIR/dist/native-topology"
xcrun swiftc -target arm64-apple-macos14.0 -O "$ROOT_DIR/Vendor/betterdisplaycli/betterdisplaycli.swift" -o "$ROOT_DIR/dist/betterdisplaycli"
APP="$ROOT_DIR/dist/UUDisplayAutomation.app"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
cp "$ROOT_DIR/dist/UUDisplayAutomation" "$APP/Contents/MacOS/UUDisplayAutomation"
xcrun swift "$ROOT_DIR/script/RenderAppIcon.swift" "$ROOT_DIR/dist"
iconutil -c icns "$ROOT_DIR/dist/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
python3 "$ROOT_DIR/script/build_office_cursor_payload.py" "$ROOT_DIR/dist/office-cursor-payload"
python3 - "$ROOT_DIR/dist/office-cursor-payload" "$APP/Contents/Resources/OfficeCursor" <<'PY'
import pathlib,shutil,sys
source,target=map(pathlib.Path,sys.argv[1:])
if target.exists(): shutil.rmtree(target)
shutil.copytree(source,target)
PY
/usr/bin/python3 - "$APP" <<'PY'
import plistlib,sys,pathlib
p=pathlib.Path(sys.argv[1])/'Contents'/'Info.plist'
p.write_bytes(plistlib.dumps({'CFBundleIdentifier':'local.codex.UUDisplayAutomation','CFBundleName':'屏幕接力','CFBundleDisplayName':'屏幕接力','CFBundleExecutable':'UUDisplayAutomation','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.7.1','CFBundleVersion':'25','CFBundleIconFile':'AppIcon','LSMinimumSystemVersion':'14.0','LSUIElement':True,'NSPrincipalClass':'NSApplication','NSHumanReadableCopyright':'Local display automation; includes MIT-attributed UUyc-companion AX recognition.'}))
PY
cp "$ROOT_DIR/LICENSE.md" "$APP/Contents/Resources/LICENSE.md"
cp "$ROOT_DIR/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/THIRD_PARTY_NOTICES.md"
codesign --force --sign - "$APP"
# Reused staging directories otherwise retain their first-build dates even when
# every signed file has changed; icon and launcher caches use those dates.
/usr/bin/touch "$APP/Contents" "$APP"
case "$MODE" in
  --build-only) exit 0 ;;
  --probe) exec "$APP/Contents/MacOS/UUDisplayAutomation" --probe ;;
esac
# A running installed controller must be paused/recovered before replacing its detector.
if pgrep -x UUDisplayAutomation >/dev/null; then
  echo 'A detector is already running. Pause/recover it before rebuilding and relaunching.' >&2
  exit 1
fi
case "$MODE" in
  run) open -g "$APP" ;;
  --verify) open -g "$APP"; sleep 2; pgrep -x UUDisplayAutomation ;;
  --debug) lldb -- "$APP/Contents/MacOS/UUDisplayAutomation" ;;
  --logs|--telemetry) open -g "$APP"; /usr/bin/log stream --info --style compact --predicate 'process == "UUDisplayAutomation"' ;;
  *) echo 'usage: build_and_run.sh [run|--build-only|--cursor-only|--office-relay-only|--probe|--verify|--debug|--logs|--telemetry]' >&2; exit 2 ;;
esac
