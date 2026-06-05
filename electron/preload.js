'use strict'

// Secure bridge between the admin web page (served by the Python backend over
// http) and the Electron main process. The admin page is loaded from a network
// origin, so it runs with contextIsolation and no Node access; this preload is
// the *only* surface it can use to talk to the shell, and it exposes a small,
// explicit, promise-based API — nothing more.
//
// It defines `window.seventhslide`, the API the admin page uses to push outputs
// to physical monitors. In a plain browser / OBS the object is simply undefined,
// and the Screens UI hides itself.

const { contextBridge, ipcRenderer } = require('electron')

/** Subscribe to a main->renderer push channel; returns an unsubscribe function. */
function subscribe(channel, callback) {
  const listener = (_event, payload) => callback(payload)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('seventhslide', {
  // Marker the admin page checks to decide whether to render desktop-only UI.
  isDesktop: true,
  platform: process.platform,

  // --- Display & output queries (request/response) ---
  listDisplays: () => ipcRenderer.invoke('displays:list'),
  listOpenOutputs: () => ipcRenderer.invoke('outputs:listOpen'),
  // { outputName: bool } — whether each output's local fullscreen window is muted.
  listMuted: () => ipcRenderer.invoke('outputs:listMuted'),

  // --- Commands ---
  // Send an output fullscreen to a display; resolves with the chosen displayId.
  openOutput: (outputName, displayId) =>
    ipcRenderer.invoke('output:open', { outputName, displayId }),
  closeOutput: (outputName) => ipcRenderer.invoke('output:close', { outputName }),
  closeAllOutputs: () => ipcRenderer.invoke('outputs:closeAll'),
  // Mute/unmute audio on the LOCAL fullscreen output window only. Other clients
  // (browsers, OBS) connected to the same output page are unaffected.
  setOutputMuted: (outputName, muted) =>
    ipcRenderer.invoke('output:setMuted', { outputName, muted: !!muted }),

  // --- Live change notifications (push) ---
  // Fired when monitors are connected/disconnected or their geometry changes.
  onDisplaysChanged: (cb) => subscribe('displays:changed', cb),
  // Fired when the set of open output windows changes (opened/closed/moved).
  onOutputsChanged: (cb) => subscribe('outputs:changed', cb),
})
