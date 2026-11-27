const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("parking", {
  bootstrap: () => ipcRenderer.invoke("parking:bootstrap"),
  newExport: () => ipcRenderer.invoke("parking:new-export"),
  pickLedger: () => ipcRenderer.invoke("parking:pick-ledger"),
  pickOutput: () => ipcRenderer.invoke("parking:pick-output"),
  verify: () => ipcRenderer.invoke("parking:verify"),
  start: (request) => ipcRenderer.invoke("parking:start", request),
  resume: (id) => ipcRenderer.invoke("parking:resume", id),
  cancel: () => ipcRenderer.invoke("parking:cancel"),
  openPath: (filePath) => ipcRenderer.invoke("parking:open-path", filePath),
  subscribe: (listener) => {
    if (typeof listener !== "function") throw new TypeError("Expected a listener");
    const handle = (_, state) => listener(state);
    ipcRenderer.on("parking:state", handle);
    return () => ipcRenderer.removeListener("parking:state", handle);
  },
});
