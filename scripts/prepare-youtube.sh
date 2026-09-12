#!/usr/bin/env bash
# Pinned experimental engine: see yt-dlp/yt-dlp#13515. Fail closed on asset replacement.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT_DIR/bundled-bin/youtube}"
CACHE="$(mktemp -d)"
trap 'rm -rf "$CACHE"' EXIT
mkdir -p "$DEST"
PLATFORM="${2:-mac}"
ARCH="${3:-arm64}"
ASSET=yt-dlp_macos
BINARY=yt-dlp-sabr
SHA=7f0d5099da22e5d6d17b11a57883d58b1701dccebfadc236193d4444652f3648
if [[ "$PLATFORM" == win ]]; then
  BINARY=yt-dlp-sabr.exe
  if [[ "$ARCH" == arm64 ]]; then
    ASSET=yt-dlp_arm64.exe
    SHA=a879f282c4b8bb69d361bce9906ae99d5372c07afc38ea4c0fb948c71cbf3c2b
  else
    ASSET=yt-dlp.exe
    SHA=b1726090e91705e598db130ddff35e0ef09b7929d064c9c3238bc76a41998bf4
  fi
fi
curl -fL --retry 3 "https://github.com/bashonly/yt-dlp/releases/download/sabr/$ASSET" -o "$CACHE/$BINARY"
python3 -c 'import hashlib,sys; assert hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()==sys.argv[2], "Enhanced engine checksum mismatch"' "$CACHE/$BINARY" "$SHA"
git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$CACHE/provider"
(cd "$CACHE/provider/server" && npm ci && npx tsc && npm prune --omit=dev)
cp "$CACHE/$BINARY" "$DEST/$BINARY"
chmod +x "$DEST/$BINARY"
mkdir -p "$DEST/server"
cp -R "$CACHE/provider/plugin" "$DEST/"
cp -R "$CACHE/provider/server/build" "$CACHE/provider/server/node_modules" "$CACHE/provider/server/package.json" "$DEST/server/"
cp "$CACHE/provider/LICENSE" "$DEST/LICENSE-bgutil"
python3 "$ROOT_DIR/scripts/patch-youtube-provider.py" "$DEST"

if [[ "$PLATFORM" == win ]]; then
  # Pin a matching x64 Node/canvas pair; Windows ARM64 can run this helper under emulation.
  node -e 'require("fs").copyFileSync(process.execPath, require("path").join(process.argv[1], "node.exe"))' "$DEST"
fi
