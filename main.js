const { app, BrowserWindow, dialog, shell, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");
const net = require("net");

let mainWindow = null;
let flaskProcess = null;
const DEFAULT_PORT = 8899;
let PORT = DEFAULT_PORT; // Actual port used, may differ from DEFAULT_PORT if occupied

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
  // 1. Already set in environment — trust whatever the caller configured
  if (process.env.https_proxy || process.env.http_proxy)
    return process.env.https_proxy || process.env.http_proxy;

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
          const proxy = `http://${match[1].trim()}`;
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
// In dev mode: prefer venv Python, fall back to system python3.
function getPythonPath() {
  if (app.isPackaged) {
    const binDir = getBinDir();
    const name = process.platform === "win32" ? "mediadrop-server.exe" : "mediadrop-server";
    const exe = path.join(binDir, name);
    if (fs.existsSync(exe)) return exe;
  }
  const venvPython = path.join(getResourcePath(), "venv", "bin", "python3");
  if (fs.existsSync(venvPython)) return venvPython;
  return "python3";
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
      { cmd: "python3", args: ["--version"], name: "Python 3" },
      { cmd: "yt-dlp", args: ["--version"], name: "yt-dlp" },
      { cmd: "ffmpeg", args: ["-version"], name: "ffmpeg" },
    ];

    let missing = [];
    let checked = 0;

    checks.forEach(({ cmd, args, name }) => {
      const proc = spawn(cmd, args, { stdio: "pipe" });
      proc.on("error", () => {
        missing.push(name);
        checked++;
        if (checked === checks.length) resolve(missing);
      });
      proc.on("close", (code) => {
        if (code !== 0) missing.push(name);
        checked++;
        if (checked === checks.length) resolve(missing);
      });
    });
  });
}

// Safety net: kill any process still holding our port on app exit.
// Not used during startup — findAvailablePort handles port conflicts non-destructively.
function killPortProcess(port) {
  if (typeof port !== 'number' || port < 1 || port > 65535) return;
  try {
    const { execSync } = require("child_process");
    const output = execSync(`lsof -ti :${port}`, { encoding: "utf8", timeout: 3000 }).trim();
    if (output) {
      const pids = output.split("\n").filter(Boolean);
      for (const pid of pids) {
        try {
          process.kill(parseInt(pid), "SIGKILL");
          console.log(`[cleanup] Killed old process ${pid} on port ${port}`);
        } catch {}
      }
    }
  } catch {}
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
    const flaskEnv = { ...process.env, PORT: String(PORT), HOST: "127.0.0.1" };
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

    flaskProcess = spawn(python, args, { cwd, env: flaskEnv, stdio: ["ignore", "pipe", "pipe"] });

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
    const res = await fetch(`http://127.0.0.1:${PORT}/api/config`);
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
    },
  });

  // Cache-bust so Chromium always loads the latest template
  mainWindow.webContents.session.clearCache().catch(() => {});
  mainWindow.loadURL(`http://127.0.0.1:${PORT}?v=${Date.now()}`);

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
    shell.openExternal(url);
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

ipcMain.handle("detect-proxy", () => {
  const url = detectProxy();
  return url || "";
});

// --- App lifecycle ---

app.whenReady().then(async () => {
  const missing = await checkDependencies();

  if (missing.length > 0) {
    const message = app.isPackaged
      ? `Bundled dependencies are missing: ${missing.join(", ")}\n\nThis is a packaging error. Please reinstall MediaDrop.`
      : `MediaDrop requires the following to be installed:\n\n${missing.join(", ")}\n\nInstall with:\n  brew install ${missing.map((m) => m.toLowerCase().replace(" ", "-")).join(" ")}`;
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
app.on("window-all-closed", () => {
  if (flaskProcess) {
    // Kill Flask and all its child processes (yt-dlp)
    try {
      process.kill(-flaskProcess.pid, "SIGTERM");
    } catch {
      flaskProcess.kill("SIGTERM");
    }
    flaskProcess = null;
  }
  // Also kill any process on our port (safety net)
  killPortProcess(PORT);
  app.quit();
});

// macOS dock click — reopen window if all were closed
app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  }
});
