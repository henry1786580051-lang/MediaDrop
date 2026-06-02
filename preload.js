const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  selectCookieFile: () => ipcRenderer.invoke("select-cookie-file"),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  detectProxy: () => ipcRenderer.invoke("detect-proxy"),
});
