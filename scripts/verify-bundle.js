const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const appBinDir = path.join(root, "dist", "mac-arm64", "MediaDrop.app", "Contents", "Resources", "bin");
const required = ["mediadrop-server", "yt-dlp", "ffmpeg"];
const requiredFonts = [
  path.join("_internal", "fonts", "Roboto-Medium.ttf"),
  path.join("_internal", "fonts", "NotoSansCJKsc-Regular.otf"),
  path.join("_internal", "fonts", "Roboto-OFL.txt"),
  path.join("_internal", "fonts", "NotoSansCJK-OFL.txt"),
];

for (const name of required) {
  const file = path.join(appBinDir, name);
  if (!fs.existsSync(file)) {
    throw new Error(`Missing bundled binary: ${file}`);
  }
  fs.accessSync(file, fs.constants.X_OK);
}

for (const relative of requiredFonts) {
  const file = path.join(appBinDir, relative);
  if (!fs.existsSync(file)) {
    throw new Error(`Missing bundled subtitle font resource: ${file}`);
  }
}

console.log(`Bundle verification passed: ${required.join(", ")} and subtitle fonts`);
