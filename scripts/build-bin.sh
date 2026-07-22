#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
BIN_DIST="$ROOT_DIR/bin-dist"
BUNDLED_BIN="$ROOT_DIR/bundled-bin"
YTDLP_VERSION="${YTDLP_VERSION:-2026.07.04}"

rm -rf "$BIN_DIST/mediadrop-server" "$BUNDLED_BIN"
mkdir -p "$BUNDLED_BIN"

(
  cd "$APP_DIR"
  python3 -m PyInstaller -y mediadrop-server.spec --distpath "$BIN_DIST"
)

cp -R "$BIN_DIST/mediadrop-server/." "$BUNDLED_BIN/"

case "$(uname -s)" in
  Darwin)
    YTDLP_ASSET="yt-dlp_macos"
    FFMPEG_URL="https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-darwin-arm64.gz"
    ;;
  *)
    echo "Unsupported local build platform: $(uname -s)" >&2
    exit 1
    ;;
esac

curl --fail --location --retry 3 -o "$BUNDLED_BIN/yt-dlp" "https://github.com/yt-dlp/yt-dlp/releases/download/$YTDLP_VERSION/$YTDLP_ASSET"
curl --fail --location --retry 3 -o "$BUNDLED_BIN/ffmpeg.gz" "$FFMPEG_URL"
gunzip -f "$BUNDLED_BIN/ffmpeg.gz"

chmod +x "$BUNDLED_BIN/mediadrop-server" "$BUNDLED_BIN/yt-dlp" "$BUNDLED_BIN/ffmpeg"

"$BUNDLED_BIN/yt-dlp" --version
"$BUNDLED_BIN/ffmpeg" -version >/dev/null
"$BUNDLED_BIN/ffmpeg" -hide_banner -filters 2>&1 | grep -E ' (ass|subtitles) '
"$BUNDLED_BIN/ffmpeg" -hide_banner -encoders 2>&1 | grep -E 'libx264|libvpx-vp9'
"$BUNDLED_BIN/ffmpeg" -hide_banner -encoders 2>&1 | grep 'h264_videotoolbox'
test -f "$BUNDLED_BIN/_internal/fonts/Roboto-Medium.ttf"
test -f "$BUNDLED_BIN/_internal/fonts/NotoSansCJKsc-Regular.otf"
test -f "$BUNDLED_BIN/_internal/fonts/Roboto-OFL.txt"
test -f "$BUNDLED_BIN/_internal/fonts/NotoSansCJK-OFL.txt"
