const {
  appendBoundedText,
  compareVersions,
  ensureSingleInstance,
  getDevelopmentPythonCandidates,
  isSafeExternalUrl,
  isSupportedProxyUrl,
  isTrustedMediaDropReleaseUrl,
  parseServerReadyPort,
  parseWindowsProxyServer,
  selectReleaseAsset,
  stopProcessTree,
} = require("../electron-utils");
const { adHocSignApp } = require("./adhoc-sign");
const { verifyMacAppSignature } = require("./verify-bundle");
const { detectExecutableArchitecture, hasEncoder } = require("./verify-ffmpeg");
const fs = require("fs");
const path = require("path");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(isSafeExternalUrl("https://github.com/example"), "HTTPS links should be allowed");
assert(isSafeExternalUrl("http://example.com"), "HTTP links should be allowed");
assert(!isSafeExternalUrl("file:///etc/passwd"), "Local file links must be blocked");
assert(!isSafeExternalUrl("javascript:alert(1)"), "Script URLs must be blocked");
assert(!isSafeExternalUrl("not a url"), "Malformed URLs must be blocked");
assert(isSupportedProxyUrl("http://127.0.0.1:7897"), "HTTP proxy URL was rejected");
assert(isSupportedProxyUrl("socks5://localhost:1080"), "SOCKS proxy URL was rejected");
assert(!isSupportedProxyUrl("http://localhost"), "Proxy without a port was accepted");
assert(!isSupportedProxyUrl("file:///tmp/socket"), "Unsafe proxy scheme was accepted");
assert(parseWindowsProxyServer("127.0.0.1:7897") === "http://127.0.0.1:7897", "Simple Windows proxy was not normalized");
assert(parseWindowsProxyServer("http=127.0.0.1:8080;https=127.0.0.1:8443") === "http://127.0.0.1:8443", "Protocol-specific Windows proxy was not parsed");
assert(parseWindowsProxyServer("socks=127.0.0.1:1080") === "socks5://127.0.0.1:1080", "Windows SOCKS proxy was not parsed");
assert(parseWindowsProxyServer("http=invalid") === null, "Invalid Windows proxy was accepted");
assert(getDevelopmentPythonCandidates("C:\\app", "win32")[0].endsWith("Scripts/python.exe") || getDevelopmentPythonCandidates("C:\\app", "win32")[0].endsWith("Scripts\\python.exe"), "Windows venv Python path is incorrect");
assert(getDevelopmentPythonCandidates("C:\\app", "win32").at(-1) === "python", "Windows Python fallback is incorrect");
assert(compareVersions("V1.1.0", "1.0.7") > 0, "New release version was not detected");
assert(compareVersions("1.0.7", "V1.0.7") === 0, "Equivalent versions did not match");
assert(compareVersions("1.1.0-beta.1", "1.1.0") < 0, "Prerelease ordering is incorrect");
assert(appendBoundedText("abc", "def", 8) === "abcdef", "Short server logs were changed");
assert(appendBoundedText("abcdef", "ghijk", 8) === "defghijk", "Server log buffer did not keep the newest output");
assert(appendBoundedText("abc", "def", 0) === "", "Disabled server log buffer retained output");
assert(parseServerReadyPort("[startup] MEDIADROP_READY 127.0.0.1:49152") === 49152, "Server ready port was not parsed");
assert(parseServerReadyPort("[startup] MEDIADROP_READY 0.0.0.0:49152") === null, "Non-loopback server marker was accepted");
assert(parseServerReadyPort("[startup] MEDIADROP_READY 127.0.0.1:70000") === null, "Invalid server port was accepted");
assert(hasEncoder(" V....D png  PNG image", "png"), "PNG encoder was not detected");
assert(hasEncoder(" A....D libmp3lame  MP3", "libmp3lame"), "MP3 encoder was not detected");
assert(!hasEncoder(" V....D apng  Animated PNG", "png"), "APNG was mistaken for the PNG encoder");

const assets = [
  { name: "MediaDrop-1.1.0-arm64.dmg", browser_download_url: "https://example.com/mac" },
  { name: "MediaDrop.Setup.1.1.0.arm64.exe", browser_download_url: "https://example.com/win-arm" },
  { name: "MediaDrop.Setup.1.1.0.exe", browser_download_url: "https://example.com/win-x64" },
];
assert(selectReleaseAsset(assets, "darwin", "arm64")?.name.endsWith("arm64.dmg"), "macOS ARM64 asset mismatch");
assert(selectReleaseAsset(assets, "win32", "arm64")?.name.endsWith("arm64.exe"), "Windows ARM64 asset mismatch");
assert(selectReleaseAsset(assets, "win32", "x64")?.name === "MediaDrop.Setup.1.1.0.exe", "Windows x64 asset mismatch");
assert(isTrustedMediaDropReleaseUrl("https://github.com/henry1786580051-lang/MediaDrop/releases/download/V1.1.0/file.exe"), "Official release URL was rejected");
assert(!isTrustedMediaDropReleaseUrl("https://github.com/other/repo/releases/download/V1/file.exe"), "Foreign release URL was trusted");
stopProcessTree(null);

let secondaryQuit = false;
assert(!ensureSingleInstance({
  requestSingleInstanceLock: () => false,
  quit: () => { secondaryQuit = true; },
}, () => null), "Secondary instance was allowed to start");
assert(secondaryQuit, "Secondary instance was not closed");
let secondInstance;
const focusActions = [];
assert(ensureSingleInstance({
  requestSingleInstanceLock: () => true,
  on: (event, callback) => { if (event === "second-instance") secondInstance = callback; },
}, () => ({
  isDestroyed: () => false, isMinimized: () => true,
  restore: () => focusActions.push("restore"), show: () => focusActions.push("show"),
  focus: () => focusActions.push("focus"),
})), "Primary instance was rejected");
secondInstance();
assert(focusActions.join(",") === "restore,show,focus", "Existing window was not restored and focused");

const packageConfig = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
assert(packageConfig.engines.node === ">=22.12.0", "Node runtime requirement is not pinned");
assert(packageConfig.devDependencies.electron === "41.10.3", "Electron version is not pinned");
assert(packageConfig.devDependencies["electron-builder"] === "26.15.3", "electron-builder version is not pinned");
assert(!packageConfig.build.files.some((entry) => entry.startsWith("app/")), "Web source leaked into Electron app.asar");
assert(!packageConfig.build.extraResources.some((entry) => entry.to === "app"), "Web resources were packaged twice");
assert(packageConfig.build.extraResources.some((entry) => entry.to === "bin"), "Bundled runtime binaries are missing");
const pyinstallerSpec = fs.readFileSync(path.join(__dirname, "..", "app", "mediadrop-server.spec"), "utf8");
assert(pyinstallerSpec.includes("('templates', 'templates')"), "PyInstaller no longer bundles templates");
assert(pyinstallerSpec.includes("('static', 'static')"), "PyInstaller no longer bundles static files");
const flaskSource = fs.readFileSync(path.join(__dirname, "..", "app", "app.py"), "utf8");
assert(!flaskSource.includes("'unsafe-inline'"), "Content security policy still allows inline code");
assert(flaskSource.includes("script-src 'self';") && flaskSource.includes("style-src 'self';"), "Strict local script and style policy is missing");
const mainSource = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
assert(mainSource.includes("if (!isPrimaryInstance) return;"), "Secondary instance can still start a backend");
assert(mainSource.includes("MEDIADROP_JS_RUNTIME: process.execPath"), "Electron is not exposed as yt-dlp's JavaScript runtime");
assert(flaskSource.includes('"ELECTRON_RUN_AS_NODE"'), "yt-dlp JavaScript runtime mode is not enabled");
assert(flaskSource.includes('"--js-runtimes"'), "yt-dlp commands do not receive the JavaScript runtime");

const buildWorkflow = fs.readFileSync(path.join(__dirname, "..", ".github", "workflows", "build.yml"), "utf8");
assert(!buildWorkflow.includes("node-version: 20"), "Release workflow still uses unsupported Node 20");
assert(buildWorkflow.includes("node-version: 22"), "Release workflow does not use Node 22");
assert(buildWorkflow.includes("pip install -r app/requirements-build.txt"), "Release workflow does not use pinned Python build dependencies");
assert(!buildWorkflow.includes("pip install flask pyinstaller"), "Release workflow still installs moving Python build dependencies");
assert(buildWorkflow.includes("FFMPEG_VERSION: '8.1.2'"), "Release workflow FFmpeg version is not pinned");
assert(buildWorkflow.includes("YTDLP_VERSION: '2026.08.19'"), "Release workflow yt-dlp version is not pinned");
assert(!buildWorkflow.includes("ffmpeg-master-latest"), "Release workflow still uses a moving FFmpeg master build");
assert(buildWorkflow.includes("FFMPEG_WINDOWS_RELEASE: 'autobuild-2026-08-17-13-05'"), "Windows FFmpeg release is not immutable");
assert(!buildWorkflow.includes("releases/download/latest/${ASSET}"), "Windows FFmpeg download still uses a moving release");
assert(buildWorkflow.includes("ffmpeg-n8.1.2-44-g7c533d0f86-win64-gpl-8.1.zip"), "Windows x64 FFmpeg asset is missing");
assert(buildWorkflow.includes("ffmpeg-n8.1.2-44-g7c533d0f86-winarm64-gpl-8.1.zip"), "Windows ARM64 FFmpeg asset is missing");
assert(buildWorkflow.includes("19b9b43e6df8839473ba22c8e22bf14b937c1e2ca40ecbd19d58afedc83ac908"), "Windows x64 FFmpeg checksum is missing");
assert(buildWorkflow.includes("bedbdfaf298650e9fc25f35c14e948db37b969d7a3bf973e7b6861ef62444031"), "Windows ARM64 FFmpeg checksum is missing");

const macFfmpegBuild = fs.readFileSync(path.join(__dirname, "build-ffmpeg-macos.sh"), "utf8");
assert(macFfmpegBuild.includes("FFMPEG_VERSION=\"8.1.2\""), "macOS FFmpeg version is not pinned");
assert(macFfmpegBuild.includes("https://codeload.github.com/FFmpeg/FFmpeg/"), "Official FFmpeg repository source is missing");
assert(macFfmpegBuild.includes("9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"), "Official FFmpeg source checksum is missing");
assert(macFfmpegBuild.includes("ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e"), "LAME source checksum is missing");
assert(macFfmpegBuild.includes("--enable-libmp3lame"), "macOS FFmpeg build does not enable MP3 encoding");
assert(macFfmpegBuild.includes("--enable-zlib"), "macOS FFmpeg build does not enable PNG encoding support");
assert(macFfmpegBuild.includes("macos-arm64-v3"), "macOS FFmpeg cache was not invalidated after adding FFprobe");
assert(macFfmpegBuild.includes("--enable-ffprobe") && !macFfmpegBuild.includes("--disable-ffprobe"), "macOS build does not include FFprobe");
assert(macFfmpegBuild.includes('cp ffprobe "$BINARY_CACHE/ffprobe"'), "FFprobe is missing from the macOS binary cache");
assert(buildWorkflow.includes("cp bundled-ffmpeg/*/bin/ffprobe.exe bundled-bin/ffprobe.exe"), "Windows build does not include FFprobe");
assert(buildWorkflow.includes("verify-ffmpeg.js bundled-bin/ffmpeg.exe"), "Windows FFmpeg architecture verification is missing");
assert(buildWorkflow.includes("npm run verify:mac"), "macOS release artifacts are not verified before upload");

const signCalls = [];
adHocSignApp("/tmp/Media Drop.app", (executable, args) => signCalls.push([executable, args]));
assert(signCalls.length === 5, "macOS signing pipeline did not run all required checks");
assert(signCalls[0][0] === "/usr/bin/xattr" && signCalls[0][1][0] === "-cr", "macOS extended attributes are not cleared before signing");
assert(signCalls[1][1].includes("com.apple.FinderInfo"), "macOS FinderInfo is not explicitly removed");
assert(signCalls[2][1].includes("com.apple.ResourceFork"), "macOS resource forks are not explicitly removed");
assert(signCalls[3][0] === "/usr/bin/codesign" && signCalls[3][1].includes("--sign"), "macOS app is not ad-hoc signed");
assert(signCalls[4][1].includes("--verify") && signCalls[4][1].includes("--strict"), "macOS signature is not verified after signing");
let signingFailurePropagated = false;
try {
  adHocSignApp("/tmp/MediaDrop.app", () => {
    throw new Error("simulated signing failure");
  });
} catch (error) {
  signingFailurePropagated = error.message === "simulated signing failure";
}
assert(signingFailurePropagated, "macOS signing failures can still be ignored");

const verificationCalls = [];
verifyMacAppSignature("/tmp/MediaDrop.app", (executable, args, options) => {
  verificationCalls.push([executable, args, options]);
});
assert(verificationCalls[0][1].includes("--deep") && verificationCalls[0][1].includes("--strict"), "macOS bundle verification is incomplete");

const localMacBuild = fs.readFileSync(path.join(__dirname, "build-macos.sh"), "utf8");
assert(packageConfig.scripts["dist:mac"] === "bash scripts/build-macos.sh", "Local macOS build does not use the isolated build script");
assert(localMacBuild.includes("mktemp -d") && localMacBuild.includes("-c.directories.output"), "Local macOS build is not isolated from File Provider metadata");
assert(localMacBuild.includes("requirements-build.txt") && localMacBuild.includes("build-venv"), "Local macOS build does not use pinned Python dependencies");
assert(localMacBuild.includes("verify-bundle.js") && localMacBuild.includes("hdiutil verify"), "Local macOS artifacts are not fully verified");
const localBinaryBuild = fs.readFileSync(path.join(__dirname, "build-bin.sh"), "utf8");
assert(localBinaryBuild.includes('PYTHON_BIN="${PYTHON_BIN:-python3}"'), "Local binary build cannot use the pinned build environment");
assert(localBinaryBuild.includes("SHA2-256SUMS") && localBinaryBuild.includes("shasum -a 256 -c -"), "Local yt-dlp download is not checksum verified");
assert(localBinaryBuild.includes('"$actual_ytdlp_version" != "$YTDLP_VERSION"'), "Local yt-dlp version is not verified");

const runtimeRequirements = fs.readFileSync(path.join(__dirname, "..", "app", "requirements.txt"), "utf8");
const buildRequirements = fs.readFileSync(path.join(__dirname, "..", "app", "requirements-build.txt"), "utf8");
assert(runtimeRequirements.includes("Flask==3.1.3"), "Flask runtime version is not pinned");
assert(runtimeRequirements.includes("yt-dlp==2026.8.19"), "yt-dlp runtime version is not pinned");
assert(buildRequirements.includes("PyInstaller==6.21.0"), "PyInstaller build version is not pinned");

const fakePe = Buffer.alloc(256);
fakePe.write("MZ", 0, "ascii");
fakePe.writeUInt32LE(128, 0x3c);
fakePe.write("PE\0\0", 128, "ascii");
fakePe.writeUInt16LE(0xaa64, 132);
const fakePePath = path.join(require("os").tmpdir(), `mediadrop-pe-${process.pid}.exe`);
fs.writeFileSync(fakePePath, fakePe);
try {
  assert(detectExecutableArchitecture(fakePePath) === "arm64", "Windows ARM64 executable was not detected");
} finally {
  fs.unlinkSync(fakePePath);
}

console.log("electron utility checks OK");
