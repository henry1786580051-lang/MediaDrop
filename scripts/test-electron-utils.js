const {
  appendBoundedText,
  compareVersions,
  getDevelopmentPythonCandidates,
  isSafeExternalUrl,
  isSupportedProxyUrl,
  isTrustedMediaDropReleaseUrl,
  parseServerReadyPort,
  parseWindowsProxyServer,
  selectReleaseAsset,
  stopProcessTree,
} = require("../electron-utils");
const { detectExecutableArchitecture } = require("./verify-ffmpeg");
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

const buildWorkflow = fs.readFileSync(path.join(__dirname, "..", ".github", "workflows", "build.yml"), "utf8");
assert(!buildWorkflow.includes("node-version: 20"), "Release workflow still uses unsupported Node 20");
assert(buildWorkflow.includes("node-version: 22"), "Release workflow does not use Node 22");
assert(buildWorkflow.includes("pip install -r app/requirements-build.txt"), "Release workflow does not use pinned Python build dependencies");
assert(!buildWorkflow.includes("pip install flask pyinstaller"), "Release workflow still installs moving Python build dependencies");
assert(buildWorkflow.includes("FFMPEG_VERSION: '8.1.2'"), "Release workflow FFmpeg version is not pinned");
assert(!buildWorkflow.includes("ffmpeg-master-latest"), "Release workflow still uses a moving FFmpeg master build");
assert(buildWorkflow.includes("ffmpeg-n8.1-latest-${{ matrix.ffmpeg_arch }}-gpl-8.1.zip"), "Windows FFmpeg stable asset is missing");
assert(buildWorkflow.includes("9f4b7be573fc9a7ade892224b756f7ae733b49c4eba46c2eab77c1a76b9f36a2"), "Windows x64 FFmpeg checksum is missing");
assert(buildWorkflow.includes("11b5555e29b71908d88959f34d79c1e356f7ff6ee349c066f899146245d51bc7"), "Windows ARM64 FFmpeg checksum is missing");

const macFfmpegBuild = fs.readFileSync(path.join(__dirname, "build-ffmpeg-macos.sh"), "utf8");
assert(macFfmpegBuild.includes("FFMPEG_VERSION=\"8.1.2\""), "macOS FFmpeg version is not pinned");
assert(macFfmpegBuild.includes("https://codeload.github.com/FFmpeg/FFmpeg/"), "Official FFmpeg repository source is missing");
assert(macFfmpegBuild.includes("9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"), "Official FFmpeg source checksum is missing");
assert(macFfmpegBuild.includes("ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e"), "LAME source checksum is missing");
assert(macFfmpegBuild.includes("--enable-libmp3lame"), "macOS FFmpeg build does not enable MP3 encoding");
assert(buildWorkflow.includes("verify-ffmpeg.js bundled-bin/ffmpeg.exe"), "Windows FFmpeg architecture verification is missing");

const runtimeRequirements = fs.readFileSync(path.join(__dirname, "..", "app", "requirements.txt"), "utf8");
const buildRequirements = fs.readFileSync(path.join(__dirname, "..", "app", "requirements-build.txt"), "utf8");
assert(runtimeRequirements.includes("Flask==3.1.3"), "Flask runtime version is not pinned");
assert(runtimeRequirements.includes("yt-dlp==2026.7.4"), "yt-dlp runtime version is not pinned");
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
