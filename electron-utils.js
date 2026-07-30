const { spawnSync } = require("child_process");
const path = require("path");

function isSafeExternalUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

function compareVersions(left, right) {
  const parse = (value) => {
    const normalized = String(value || "").trim().replace(/^v/i, "");
    const [core, prerelease = ""] = normalized.split("-", 2);
    const numbers = core.split(".").map((part) => Number.parseInt(part, 10) || 0);
    return { numbers, prerelease };
  };
  const a = parse(left);
  const b = parse(right);
  const length = Math.max(a.numbers.length, b.numbers.length, 3);
  for (let index = 0; index < length; index++) {
    const difference = (a.numbers[index] || 0) - (b.numbers[index] || 0);
    if (difference) return difference > 0 ? 1 : -1;
  }
  if (a.prerelease === b.prerelease) return 0;
  if (!a.prerelease) return 1;
  if (!b.prerelease) return -1;
  return a.prerelease.localeCompare(b.prerelease);
}

function selectReleaseAsset(assets, platform, arch) {
  const list = Array.isArray(assets) ? assets : [];
  const candidates = list.filter((asset) => asset && typeof asset.name === "string");
  if (platform === "darwin") {
    if (arch === "arm64") {
      return candidates.find((asset) => /arm64.*\.dmg$|\.dmg$/i.test(asset.name) && !/x64|x86_64/i.test(asset.name)) || null;
    }
    return candidates.find((asset) => /\.dmg$/i.test(asset.name) && !/arm64|aarch64/i.test(asset.name)) || null;
  }
  if (platform === "win32") {
    if (arch === "arm64") {
      return candidates.find((asset) => /(?:arm64|aarch64).*\.exe$|\.arm64\.exe$/i.test(asset.name)) || null;
    }
    return candidates.find((asset) => /\.exe$/i.test(asset.name) && !/arm64|aarch64/i.test(asset.name)) || null;
  }
  return null;
}

function isTrustedMediaDropReleaseUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" &&
      url.pathname.startsWith("/henry1786580051-lang/MediaDrop/releases/");
  } catch {
    return false;
  }
}

function isSupportedProxyUrl(value) {
  if (!value) return false;
  try {
    const url = new URL(value);
    return ["http:", "https:", "socks4:", "socks5:", "socks5h:"].includes(url.protocol) &&
      Boolean(url.hostname && url.port);
  } catch {
    return false;
  }
}

function normalizeProxyEndpoint(value, defaultScheme = "http") {
  const endpoint = String(value || "").trim();
  if (!endpoint) return null;
  const normalized = /^[a-z][a-z0-9+.-]*:\/\//i.test(endpoint)
    ? endpoint
    : `${defaultScheme}://${endpoint}`;
  return isSupportedProxyUrl(normalized) ? normalized : null;
}

function parseWindowsProxyServer(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  if (!raw.includes("=")) return normalizeProxyEndpoint(raw);

  const entries = new Map();
  for (const part of raw.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 1) continue;
    entries.set(part.slice(0, separator).trim().toLowerCase(), part.slice(separator + 1).trim());
  }
  for (const [key, scheme] of [["https", "http"], ["http", "http"], ["socks", "socks5"]]) {
    const proxy = normalizeProxyEndpoint(entries.get(key), scheme);
    if (proxy) return proxy;
  }
  return null;
}

function getDevelopmentPythonCandidates(resourceDir, platform = process.platform) {
  if (platform === "win32") {
    return [path.join(resourceDir, "venv", "Scripts", "python.exe"), "python"];
  }
  return [path.join(resourceDir, "venv", "bin", "python3"), "python3"];
}

function stopProcessTree(child) {
  if (!child || !child.pid) return;
  if (process.platform === "win32") {
    const result = spawnSync(
      "taskkill",
      ["/pid", String(child.pid), "/t", "/f"],
      { windowsHide: true, stdio: "ignore" }
    );
    if (result.status === 0) return;
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
      return;
    } catch {}
  }
  try {
    child.kill("SIGTERM");
  } catch {}
}

module.exports = {
  compareVersions,
  getDevelopmentPythonCandidates,
  isSafeExternalUrl,
  isSupportedProxyUrl,
  isTrustedMediaDropReleaseUrl,
  normalizeProxyEndpoint,
  parseWindowsProxyServer,
  selectReleaseAsset,
  stopProcessTree,
};
