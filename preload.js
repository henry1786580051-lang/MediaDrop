const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  updateNativeGlass: appearance => ipcRenderer.invoke("native-glass", appearance),
  closeSettings: () => ipcRenderer.invoke("close-settings-window"),
  openSettings: tab => ipcRenderer.invoke("open-settings-window", tab),
  getAppearance: () => ipcRenderer.invoke("desktop-appearance"),
  previewFile: file => ipcRenderer.invoke("preview-file", file),
  openFile: file => ipcRenderer.invoke("open-media-file", file),
  dragFile: file => ipcRenderer.send("drag-media-file", file),
  onDesktopCommand: callback => { const handler = (_event, value) => callback(value); ipcRenderer.on("desktop-command", handler); return () => ipcRenderer.removeListener("desktop-command", handler); },
  onAppearance: callback => { const handler = (_event, value) => callback(value); ipcRenderer.on("desktop-appearance", handler); return () => ipcRenderer.removeListener("desktop-appearance", handler); },
  selectMediaFile: () => ipcRenderer.invoke("select-media-file"),
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  selectCookieFile: () => ipcRenderer.invoke("select-cookie-file"),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  detectProxy: () => ipcRenderer.invoke("detect-proxy"),
  showItemInFolder: (filePath) => ipcRenderer.invoke("show-item-in-folder", filePath),
  notify: (title, body) => ipcRenderer.invoke("show-notification", title, body),
  getAppUpdateState: () => ipcRenderer.invoke("get-app-update-state"),
  checkAppUpdate: () => ipcRenderer.invoke("check-app-update"),
  openAppUpdateDownload: () => ipcRenderer.invoke("open-app-update-download"),
  onAppUpdateState: (callback) => {
    const handler = (_event, state) => callback(state);
    ipcRenderer.on("app-update-state", handler);
    return () => ipcRenderer.removeListener("app-update-state", handler);
  },
});
