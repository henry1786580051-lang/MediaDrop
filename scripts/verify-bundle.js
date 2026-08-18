const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { verifyFfmpeg } = require("./verify-ffmpeg");

const root = path.join(__dirname, "..");
const defaultAppPath = path.join(root, "dist", "mac-arm64", "MediaDrop.app");

function verifyMacAppSignature(appPath, commandRunner = execFileSync) {
  commandRunner(
    "/usr/bin/codesign",
    ["--verify", "--deep", "--strict", "--verbose=2", appPath],
    { stdio: "inherit" },
  );
}

function verifyBundle(appPath = defaultAppPath) {
  const appBinDir = path.join(appPath, "Contents", "Resources", "bin");
  const required = ["mediadrop-server", "yt-dlp", "ffmpeg"];

  for (const name of required) {
    const file = path.join(appBinDir, name);
    if (!fs.existsSync(file)) {
      throw new Error(`Missing bundled binary: ${file}`);
    }
    fs.accessSync(file, fs.constants.X_OK);
  }

  verifyFfmpeg(path.join(appBinDir, "ffmpeg"), "arm64", "8.1.2");
  verifyMacAppSignature(appPath);

  console.log(`Bundle verification passed: signature, ${required.join(", ")}`);
}

if (require.main === module) {
  verifyBundle(process.argv[2]);
}

module.exports = { verifyBundle, verifyMacAppSignature };
