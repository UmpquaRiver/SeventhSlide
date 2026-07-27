'use strict'

// Preload bridge: contextIsolation + no Node in the admin page. Exposes
// `window.seventhslide` for desktop monitor control (undefined in browser/OBS).

const { contextBridge, ipcRenderer } = require('electron')

/** Subscribe to a main->renderer push channel; returns an unsubscribe function. */
function subscribe(channel, callback) {
  const listener = (_event, payload) => callback(payload)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('seventhslide', {
  isDesktop: true,
  platform: process.platform,

  listDisplays: () => ipcRenderer.invoke('displays:list'),
  listOpenOutputs: () => ipcRenderer.invoke('outputs:listOpen'),
  // { outputName: bool } — mute state of each local fullscreen window.
  listMuted: () => ipcRenderer.invoke('outputs:listMuted'),

  openOutput: (outputName, displayId) =>
    ipcRenderer.invoke('output:open', { outputName, displayId }),
  closeOutput: (outputName) => ipcRenderer.invoke('output:close', { outputName }),
  closeAllOutputs: () => ipcRenderer.invoke('outputs:closeAll'),
  // Mute applies only to the local fullscreen window, not other clients.
  setOutputMuted: (outputName, muted) =>
    ipcRenderer.invoke('output:setMuted', { outputName, muted: !!muted }),

  onDisplaysChanged: (cb) => subscribe('displays:changed', cb),
  onOutputsChanged: (cb) => subscribe('outputs:changed', cb),
})
