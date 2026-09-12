const path = require('node:path');
const os = require('node:os');
let bridge;
function supported() { return process.platform === 'darwin' && Number(os.release().split('.')[0]) >= 25; }
function load(app) {
  if (!supported()) return null;
  if (bridge === undefined) {
    try { bridge = require(app.isPackaged ? path.join(process.resourcesPath, 'native', 'liquid-glass.node') : path.join(__dirname, 'build/native/liquid-glass.node')); }
    catch (error) { bridge = null; console.warn('[appearance] Native glass unavailable:', error.message); }
  }
  return bridge;
}
function windowOptions(app) { return load(app) ? { transparent: true, backgroundColor: '#00000000' } : {}; }
function install({ app, ipcMain, BrowserWindow, nativeTheme }) {
  ipcMain.handle('native-glass', (event, appearance) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed() || event.senderFrame !== event.sender.mainFrame) return false;
    const url = new URL(event.sender.getURL());
    if (url.hostname !== '127.0.0.1') return false;
    const api = load(app);
    if (!api) return false;
    try { return api.apply(win.getNativeWindowHandle(), !nativeTheme.prefersReducedTransparency && !nativeTheme.shouldUseHighContrastColors, ['dark', 'light'].includes(appearance) ? appearance : 'system'); }
    catch (error) { console.warn('[appearance] Native glass disabled:', error.message); return false; }
  });
}
module.exports = { windowOptions, install };
