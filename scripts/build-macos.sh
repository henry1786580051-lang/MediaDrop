#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="${TMPDIR:-/tmp}"
BUILD_OUTPUT="$(mktemp -d "${TEMP_ROOT%/}/mediadrop-macos-build.XXXXXX")"

cleanup() {
  rm -rf "$BUILD_OUTPUT"
}
trap cleanup EXIT

cd "$ROOT_DIR"
python3 -m venv "$BUILD_OUTPUT/build-venv"
"$BUILD_OUTPUT/build-venv/bin/python" -m pip install \
  --disable-pip-version-check \
  -r "$ROOT_DIR/app/requirements-build.txt"
PYTHON_BIN="$BUILD_OUTPUT/build-venv/bin/python" npm run build:bin

# Desktop can be managed by macOS File Provider, which may restore FinderInfo
# while codesign is running. Assemble and sign the app outside that directory.
npx electron-builder --mac --arm64 \
  -c.electronDist="$ROOT_DIR/node_modules/electron/dist" \
  -c.directories.output="$BUILD_OUTPUT"

node scripts/verify-bundle.js "$BUILD_OUTPUT/mac-arm64/MediaDrop.app"

shopt -s nullglob
dmgs=("$BUILD_OUTPUT"/*.dmg)
if [[ ${#dmgs[@]} -ne 1 ]]; then
  echo "Expected exactly one macOS DMG, found ${#dmgs[@]}" >&2
  exit 1
fi

hdiutil verify "${dmgs[0]}"
mkdir -p "$ROOT_DIR/dist"
cp -f "${dmgs[0]}" "$ROOT_DIR/dist/"
echo "macOS release artifact: $ROOT_DIR/dist/$(basename "${dmgs[0]}")"
