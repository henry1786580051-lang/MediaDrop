const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
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
