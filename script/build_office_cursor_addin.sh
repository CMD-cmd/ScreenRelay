#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_OUT="$TASK_ROOT/dist/office-cursor-addin"
mkdir -p "$TASK_OUT"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Werror -dynamiclib \
  "$TASK_ROOT/script/OfficeCursorScaler.m" -framework AppKit \
  -Wl,-exported_symbol,_UUOfficeCursorInstall \
  -Wl,-exported_symbol,_UUOfficeCursorSetScale \
  -Wl,-exported_symbol,_UUOfficeCursorStop \
  -Wl,-exported_symbol,_UUOfficeCursorGetState \
  -o "$TASK_OUT/libUUNativeCursor.dylib"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Werror \
  "$TASK_ROOT/script/test_office_cursor_scaler.m" -framework AppKit \
  -o "$TASK_OUT/test-office-cursor-scaler"
"$TASK_OUT/test-office-cursor-scaler"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Werror \
  "$TASK_ROOT/script/OfficeCursorAcceptance.m" -framework AppKit \
  -o "$TASK_OUT/office-cursor-acceptance"
xcrun swiftc -O "$TASK_ROOT/script/OfficeCursorRelay.swift" "$TASK_ROOT/script/OfficeCursorRelayMain.swift" \
  "$TASK_ROOT/Sources/StatusFileObserver.swift" -o "$TASK_OUT/office-cursor-relay"
codesign --force --sign - "$TASK_OUT/libUUNativeCursor.dylib"
python3 "$TASK_ROOT/script/prepare_office_cursor_addin.py" "$TASK_OUT"
