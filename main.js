const { app, BrowserWindow, dialog, shell, ipcMain, net: electronNet } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");
const net = require("net");
const crypto = require("crypto");
const {
  compareVersions,
  getDevelopmentPythonCandidates,
  isSafeExternalUrl,
  isSupportedProxyUrl,
  isTrustedMediaDropReleaseUrl,
  normalizeProxyEndpoint,
  parseWindowsProxyServer,
  selectReleaseAsset,
  stopProcessTree,
} = require("./electron-utils");

let mainWindow = null;
let flaskProcess = null;
const DEFAULT_PORT = 8899;
let PORT = DEFAULT_PORT; // Actual port used, may differ from DEFAULT_PORT if occupied
const API_TOKEN = crypto.randomBytes(32).toString("hex");
const RELEASE_API_URL = "https://api.github.com/repos/henry1786580051-lang/MediaDrop/releases/latest";
const UPDATE_CHECK_INTERVAL = 6 * 60 * 60 * 1000;
let updateCheckPromise = null;
let updateState = {
  status: "idle",
  currentVersion: app.getVersion(),
};

function publishUpdateState(nextState) {
  updateState = { ...updateState, ...nextState };
  if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.webContents.isLoading()) {
    mainWindow.webContents.send("app-update-state", updateState);
  }
  return updateState;
}

function canReachProxy(proxyUrl) {
  return new Promise((resolve) => {
    if (!isSupportedProxyUrl(proxyUrl)) {
      resolve(false);
      return;
    }
    const parsed = new URL(proxyUrl);
    const socket = net.createConnection({ host: parsed.hostname, port: Number(parsed.port) });
    let settled = false;
    const finish = (reachable) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(reachable);
    };
    socket.setTimeout(1000);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

async function applyConfiguredElectronProxy() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  let proxyUrl = "";
  try {
    const response = await fetch(`http://127.0.0.1:${PORT}/api/config`, {
      headers: { "X-MediaDrop-Token": API_TOKEN },
    });
    if (response.ok) proxyUrl = String((await response.json()).proxy_url || "");
  } catch {}

  const session = mainWindow.webContents.session;
  if (proxyUrl && await canReachProxy(proxyUrl)) {
    const chromiumProxyUrl = proxyUrl.replace(/^socks5h:/i, "socks5:");
    await session.setProxy({
      mode: "fixed_servers",
      proxyRules: chromiumProxyUrl,
      proxyBypassRules: "<local>;localhost;127.0.0.1;[::1]",
    });
  } else {
    await session.setProxy({ mode: "system" });
  }
}

async function checkForAppUpdate(manual = false) {
  if (updateCheckPromise) {
    const result = await updateCheckPromise;
    return manual ? publishUpdateState({ ...result, manual: true }) : result;
  }
  publishUpdateState({ status: "checking", manual, error: null });
  updateCheckPromise = (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      await applyConfiguredElectronProxy();
      const response = await electronNet.fetch(RELEASE_API_URL, {
        headers: {
          Accept: "application/vnd.github+json",
          "User-Agent": `MediaDrop/${app.getVersion()}`,
        },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`GitHub API returned ${response.status}`);
      const release = await response.json();
      if (release.draft || release.prerelease || !release.tag_name) {
        throw new Error("Latest stable release information is unavailable");
      }
      const latestVersion = String(release.tag_name).replace(/^v/i, "");
      const common = {
        currentVersion: app.getVersion(),
        latestVersion,
        releaseName: release.name || `MediaDrop ${release.tag_name}`,
        releaseNotes: String(release.body || "").slice(0, 2000),
        releaseUrl: release.html_url,
        checkedAt: new Date().toISOString(),
        manual,
        error: null,
      };
      if (compareVersions(latestVersion, app.getVersion()) <= 0) {
        return publishUpdateState({ ...common, status: "up-to-date", downloadUrl: null });
      }
      const asset = selectReleaseAsset(release.assets, process.platform, process.arch);
      if (!asset || !isTrustedMediaDropReleaseUrl(asset.browser_download_url)) {
        return publishUpdateState({
          ...common,
          status: "unsupported",
          assetName: null,
          assetSize: null,
          downloadUrl: null,
        });
      }
      return publishUpdateState({
        ...common,
        status: "available",
        assetName: asset.name,
        assetSize: Number(asset.size) || null,
        downloadUrl: asset.browser_download_url,
      });
    } catch (error) {
      const message = error && error.name === "AbortError" ? "检查更新超时" : String(error.message || error);
      return publishUpdateState({
        status: "error",
        currentVersion: app.getVersion(),
        latestVersion: null,
        releaseUrl: null,
        assetName: null,
        assetSize: null,
        downloadUrl: null,
        checkedAt: new Date().toISOString(),
        manual,
        error: message,
      });
    } finally {
      clearTimeout(timeout);
      updateCheckPromise = null;
    }
  })();
  return updateCheckPromise;
}

// --- PATH setup (macOS only) ---
// When launched from a DMG, macOS resets PATH to a minimal set, losing Homebrew
// and user-installed Python packages. We restore them here so that `yt-dlp`,
// `ffmpeg`, and user Python scripts are discoverable by the Flask subprocess.
if (process.platform === "darwin") {
  const userSitePackages = path.join(os.homedir(), "Library/Python/3.9/lib/python/site-packages");
  const userBin = path.join(os.homedir(), "Library/Python/3.9/bin");
  for (const p of ["/opt/homebrew/bin", userBin]) {
    process.env.PATH = process.env.PATH.split(":").filter(x => x !== p).join(":");
  }
  process.env.PATH = `/opt/homebrew/bin:${userBin}:${process.env.PATH}`;
  if (!process.env.PYTHONPATH || !process.env.PYTHONPATH.includes(userSitePackages)) {
    process.env.PYTHONPATH = `${userSitePackages}:${process.env.PYTHONPATH || ""}`;
  }
}

// Detect system proxy via 3 methods (priority order). Returns proxy URL or null.
// Also sets http_proxy/https_proxy env vars so child processes inherit them.
// NOTE: Cannot detect TUN-mode proxies (e.g. Clash in enhanced mode) since they
// operate at the network layer without exposing a system proxy setting.
function detectProxy() {
  // 1. Already set in environment
  const environmentProxy = normalizeProxyEndpoint(process.env.https_proxy || process.env.http_proxy);
  if (environmentProxy) return environmentProxy;

  const { execSync } = require("child_process");

  // 2. Windows system proxy (Internet Explorer / Edge settings in registry)
  if (process.platform === "win32") {
    try {
      const enabled = execSync(
        'reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyEnable',
        { encoding: "utf8", timeout: 3000 }
      );
      if (enabled.includes("0x1")) {
        const server = execSync(
          'reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyServer',
          { encoding: "utf8", timeout: 3000 }
        );
        const match = server.match(/ProxyServer\s+REG_SZ\s+(.+)/);
        if (match) {
          const proxy = parseWindowsProxyServer(match[1]);
          if (!proxy) return null;
          process.env.http_proxy = proxy;
          process.env.https_proxy = proxy;
          console.log(`[proxy] Using Windows system proxy: ${proxy}`);
          return proxy;
        }
      }
    } catch {}
    return null;
  }

  // 3. macOS system proxy (System Settings → Wi-Fi → Proxies)
  try {
    const output = execSync("networksetup -getwebproxy Wi-Fi", { encoding: "utf8", timeout: 3000 });
    const enabled = output.match(/Enabled:\s*(Yes|No)/i);
    const server = output.match(/Server:\s*(.+)/);
    const port = output.match(/Port:\s*(\d+)/);
    if (enabled && enabled[1].toLowerCase() === "yes" && server && port) {
      const proxy = `http://${server[1].trim()}:${port[1].trim()}`;
      process.env.http_proxy = proxy;
      process.env.https_proxy = proxy;
      console.log(`[proxy] Using system proxy: ${proxy}`);
      return proxy;
    }
  } catch {}

  // 4. Shell profile fallback — source .zshrc/.bashrc to read exported proxy vars
  try {
    const shellPath = process.env.SHELL || "/bin/zsh";
    const profile = shellPath.includes("zsh") ? ".zshrc" : ".bashrc";
    if (!/^\.(zshrc|bashrc)$/.test(profile)) return null;
    const content = execSync(`source ~/${profile} 2>/dev/null; echo "$http_proxy|$https_proxy"`, {
      encoding: "utf8", timeout: 3000, shell: "/bin/zsh"
    });
    const [http, https] = content.trim().split("|");
    if (http) process.env.http_proxy = http;
    if (https) process.env.https_proxy = https;
    const proxy = https || http;
    if (proxy) console.log(`[proxy] Using profile proxy: ${proxy}`);
    return proxy || null;
  } catch {}
  return null;
}

// Try to bind startPort; if occupied, recursively try startPort+1, +2, etc.
// Uses net.createServer() to test availability without actually starting a server.
function findAvailablePort(startPort) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(startPort, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
    server.on("error", () => {
      findAvailablePort(startPort + 1).then(resolve).catch(reject);
    });
  });
}

// Resolve the app/ directory path — different locations in dev vs packaged DMG
function getResourcePath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app");
  }
  return path.join(__dirname, "app");
}

// Directory containing bundled binaries (mediadrop-server, yt-dlp, ffmpeg)
function getBinDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "bin");
  }
  return null;
}

// In packaged mode: return bundled mediadrop-server executable.
// In dev mode: prefer the platform's venv Python, then its normal PATH command.
function getPythonPath() {
  if (app.isPackaged) {
    const binDir = getBinDir();
    const name = process.platform === "win32" ? "mediadrop-server.exe" : "mediadrop-server";
    const exe = path.join(binDir, name);
    if (fs.existsSync(exe)) return exe;
  }
  const candidates = getDevelopmentPythonCandidates(getResourcePath());
  return candidates.find((candidate) => path.isAbsolute(candidate) && fs.existsSync(candidate)) || candidates.at(-1);
}

// Check required dependencies. Packaged: check bundled binaries exist on disk.
// Dev mode: spawn each tool to verify it's on PATH.
function checkDependencies() {
  return new Promise((resolve) => {
    if (app.isPackaged) {
      const binDir = getBinDir();
      const required = process.platform === "win32"
        ? ["mediadrop-server.exe", "yt-dlp.exe", "ffmpeg.exe"]
        : ["mediadrop-server", "yt-dlp", "ffmpeg"];
      const missing = required.filter(name => !fs.existsSync(path.join(binDir, name)));
      resolve(missing);
      return;
    }

    const checks = [
      { cmd: getPythonPath(), args: ["--version"], name: "Python 3" },
      { cmd: "yt-dlp", args: ["--version"], name: "yt-dlp" },
      { cmd: "ffmpeg", args: ["-version"], name: "ffmpeg" },
    ];

    Promise.all(checks.map(({ cmd, args, name }) => new Promise((done) => {
      let settled = false;
      const finish = (available) => {
        if (settled) return;
        settled = true;
        done(available ? null : name);
      };
      const proc = spawn(cmd, args, { stdio: "pipe", windowsHide: true });
      proc.once("error", () => finish(false));
      proc.once("close", (code) => finish(code === 0));
    }))).then((results) => resolve(results.filter(Boolean)));
  });
}

// Poll port with TCP connect every 300ms until Flask is ready or timeout.
function waitForPort(port, timeout = 15000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tryConnect = () => {
      const socket = new net.Socket();
      socket.once("connect", () => {
        socket.destroy();
        resolve();
      });
      socket.once("error", () => {
        socket.destroy();
        if (Date.now() - start > timeout) {
          reject(new Error("Flask server did not start in time"));
        } else {
          setTimeout(tryConnect, 300);
        }
      });
      socket.connect(port, "127.0.0.1");
    };
    tryConnect();
  });
}

// Launch Flask (or bundled mediadrop-server) as a child process.
// In packaged mode: spawns the PyInstaller binary, sets MEDIADROP_DATA_DIR to
// a writable userData directory so config.json and downloads persist.
// In dev mode: spawns python3 app.py from the app/ directory.
function startFlaskServer(proxyUrl) {
  const python = getPythonPath();

  return new Promise((resolve, reject) => {
    let stderrOutput = "";
    const flaskEnv = {
      ...process.env,
      PORT: String(PORT),
      HOST: "127.0.0.1",
      MEDIADROP_API_TOKEN: API_TOKEN,
      MEDIADROP_VERSION: app.getVersion(),
    };
    if (proxyUrl) flaskEnv.PROXY_URL = proxyUrl;

    let args, cwd;
    if (app.isPackaged) {
      // Bundled binary — no args needed, data dir goes to writable userData
      const dataDir = path.join(app.getPath("userData"), "server");
      fs.mkdirSync(dataDir, { recursive: true });
      flaskEnv.MEDIADROP_DATA_DIR = dataDir;
      args = [];
      cwd = dataDir;
    } else {
      args = [path.join(getResourcePath(), "app.py")];
      cwd = getResourcePath();
    }

    flaskProcess = spawn(python, args, {
      cwd,
      env: flaskEnv,
      stdio: ["ignore", "pipe", "pipe"],
      // Packaged apps use a separate process group for tree cleanup. In
      // development, sharing the terminal group lets Ctrl+C reach Flask too.
      detached: app.isPackaged && process.platform !== "win32",
      windowsHide: true,
    });

    flaskProcess.on("error", (err) => {
      reject(new Error(`Failed to start server: ${err.message}`));
    });

    flaskProcess.on("close", (code) => {
      if (code && code !== 0 && !mainWindow) {
        reject(new Error(`Server process exited with code ${code}\n\n${stderrOutput}`));
      }
    });

    flaskProcess.stderr.on("data", (data) => {
      stderrOutput += data.toString();
      console.error(`Flask stderr: ${data}`);
    });

    waitForPort(PORT).then(resolve).catch(reject);
  });
}

// Fetch download dir from Flask API (used by Electron's will-download handler)
async function getDownloadDir() {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/api/config`, {
      headers: { "X-MediaDrop-Token": API_TOKEN },
    });
    const data = await res.json();
    return data.download_dir || null;
  } catch {
    return null;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 750,
    title: "MediaDrop",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  // Cache-bust so Chromium always loads the latest template
  mainWindow.webContents.session.clearCache().catch(() => {});
  const localOrigin = `http://127.0.0.1:${PORT}`;
  mainWindow.webContents.session.cookies.set({
    url: localOrigin,
    name: "mediadrop_token",
    value: API_TOKEN,
    httpOnly: true,
    sameSite: "strict",
  }).then(() => mainWindow.loadURL(`${localOrigin}?v=${Date.now()}`)).catch((error) => {
    console.error(`[security] Could not establish local session: ${error.message}`);
    app.quit();
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(`${localOrigin}/`) && url !== localOrigin) event.preventDefault();
  });

  // Auto-save downloads to configured path instead of showing system dialog
  mainWindow.webContents.session.on("will-download", async (event, item) => {
    const downloadDir = await getDownloadDir();
    if (downloadDir) {
      const filename = item.getFilename();
      const savePath = path.join(downloadDir, filename);
      item.setSavePath(savePath);
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
}

// --- IPC handlers (called from renderer via preload.js) ---

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle("select-cookie-file", async () => {
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    properties: ["openFile"],
    filters: [
      { name: "Cookie files", extensions: ["txt"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle("detect-proxy", () => {
  const url = detectProxy();
  return url || "";
});

ipcMain.handle("show-item-in-folder", (_event, filePath) => {
  if (typeof filePath !== "string" || !path.isAbsolute(filePath) || !fs.existsSync(filePath)) return false;
  shell.showItemInFolder(filePath);
  return true;
});

ipcMain.handle("show-notification", (_event, title, body) => {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const { Notification } = require("electron");
  if (!Notification.isSupported()) return false;
  new Notification({ title: String(title).slice(0, 80), body: String(body).slice(0, 200) }).show();
  return true;
});

ipcMain.handle("get-app-update-state", () => updateState);

ipcMain.handle("check-app-update", () => checkForAppUpdate(true));

ipcMain.handle("open-app-update-download", async () => {
  const target = updateState.downloadUrl || updateState.releaseUrl;
  if (!target || !isTrustedMediaDropReleaseUrl(target)) return false;
  await shell.openExternal(target);
  return true;
});

// --- App lifecycle ---

app.whenReady().then(async () => {
  const missing = await checkDependencies();

  if (missing.length > 0) {
    const installHint = process.platform === "darwin"
      ? `Install with:\n  brew install ${missing.map((m) => m.toLowerCase().replace(" ", "-")).join(" ")}`
      : "Install Python 3, yt-dlp, and FFmpeg, and make sure they are available on PATH.";
    const message = app.isPackaged
      ? `Bundled dependencies are missing: ${missing.join(", ")}\n\nThis is a packaging error. Please reinstall MediaDrop.`
      : `MediaDrop requires the following to be installed:\n\n${missing.join(", ")}\n\n${installHint}`;
    await dialog.showMessageBox({
      type: "error",
      title: "Missing Dependencies",
      message,
      buttons: ["OK"],
    });
    app.quit();
    return;
  }

  try {
    const proxyUrl = detectProxy();
    PORT = await findAvailablePort(DEFAULT_PORT);
    if (PORT !== DEFAULT_PORT) {
      console.log(`[port] ${DEFAULT_PORT} occupied, using ${PORT}`);
    }
    await startFlaskServer(proxyUrl);
    createWindow();
    const firstUpdateCheck = setTimeout(() => checkForAppUpdate(false), 30000);
    firstUpdateCheck.unref();
    const updateTimer = setInterval(() => checkForAppUpdate(false), UPDATE_CHECK_INTERVAL);
    updateTimer.unref();
  } catch (err) {
    await dialog.showMessageBox({
      type: "error",
      title: "Startup Error",
      message: `Failed to start MediaDrop server:\n\n${err.message}`,
      buttons: ["OK"],
    });
    app.quit();
  }
});

// Cleanup: kill Flask process group (includes all yt-dlp children) on window close
function stopFlaskServer() {
  if (flaskProcess) {
    stopProcessTree(flaskProcess);
    flaskProcess = null;
  }
}

app.on("before-quit", stopFlaskServer);
app.on("window-all-closed", () => {
  stopFlaskServer();
  app.quit();
});

for (const signalName of ["SIGINT", "SIGTERM"]) {
  process.once(signalName, () => {
    stopFlaskServer();
    app.quit();
  });
}

// macOS dock click — reopen window if all were closed
app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  }
});
