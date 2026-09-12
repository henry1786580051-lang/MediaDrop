#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || exit 0
HEADERS="$(mktemp -d)"
trap 'rm -rf "$HEADERS"' EXIT
for name in node_api.h node_api_types.h js_native_api.h js_native_api_types.h; do
  curl -fsSL --retry 3 "https://raw.githubusercontent.com/nodejs/node/v22.22.0/src/$name" -o "$HEADERS/$name"
done
mkdir -p "$ROOT_DIR/build/native"
xcrun clang++ -std=c++17 -fobjc-arc -shared -undefined dynamic_lookup \
  -DNAPI_VERSION=8 -DNODE_GYP_MODULE_NAME=mediadrop_glass \
  -mmacosx-version-min=11.0 -isysroot "$(xcrun --show-sdk-path)" \
  -I "$HEADERS" -framework AppKit \
  "$ROOT_DIR/native/liquid-glass.mm" -o "$ROOT_DIR/build/native/liquid-glass.node"
