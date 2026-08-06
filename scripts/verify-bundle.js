const fs = require("fs");
const path = require("path");
const { verifyFfmpeg } = require("./verify-ffmpeg");

const root = path.join(__dirname, "..");
const appBinDir = path.join(root, "dist", "mac-arm64", "MediaDrop.app", "Contents", "Resources", "bin");
const required = ["mediadrop-server", "yt-dlp", "ffmpeg"];

for (const name of required) {
  const file = path.join(appBinDir, name);
  if (!fs.existsSync(file)) {
    throw new Error(`Missing bundled binary: ${file}`);
  }
  fs.accessSync(file, fs.constants.X_OK);
}

verifyFfmpeg(path.join(appBinDir, "ffmpeg"), "arm64", "8.1.2");

console.log(`Bundle verification passed: ${required.join(", ")}`);
