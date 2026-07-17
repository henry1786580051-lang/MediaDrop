const {
  compareVersions,
  isSafeExternalUrl,
  isSupportedProxyUrl,
  isTrustedMediaDropReleaseUrl,
  selectReleaseAsset,
  stopProcessTree,
} = require("../electron-utils");
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
assert(compareVersions("V1.1.0", "1.0.7") > 0, "New release version was not detected");
assert(compareVersions("1.0.7", "V1.0.7") === 0, "Equivalent versions did not match");
assert(compareVersions("1.1.0-beta.1", "1.1.0") < 0, "Prerelease ordering is incorrect");

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
assert(!packageConfig.build.files.some((entry) => entry.startsWith("app/")), "Web source leaked into Electron app.asar");
assert(!packageConfig.build.extraResources.some((entry) => entry.to === "app"), "Web resources were packaged twice");
assert(packageConfig.build.extraResources.some((entry) => entry.to === "bin"), "Bundled runtime binaries are missing");
const pyinstallerSpec = fs.readFileSync(path.join(__dirname, "..", "app", "mediadrop-server.spec"), "utf8");
assert(pyinstallerSpec.includes("('templates', 'templates')"), "PyInstaller no longer bundles templates");
assert(pyinstallerSpec.includes("('static', 'static')"), "PyInstaller no longer bundles static files");

console.log("electron utility checks OK");
