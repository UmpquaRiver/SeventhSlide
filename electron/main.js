'use strict'

// SeventhSlide desktop shell (Electron).
//
// What this process does, in order:
//   1. Enforce a single running instance.
//   2. Start the Python backend on the fixed port (49777) and wait until ready.
//   3. Open the admin control window (Chromium-rendered) pointing at /admin.
//   4. Expose a small IPC API (see preload.js) so the admin page can send outputs
//      fullscreen to physical monitors, via OutputManager.
//   5. Restore the previous session's screen assignments.
//   6. Tear everything down cleanly on quit.
//
// The shell is purely additive: the Python backend and the web UI
// (templates/admin.html, output.html) are the same app a browser or OBS connects
// to. `window.seventhslide` (preload.js) is the only extra surface — it lets the
// admin page push outputs to physical monitors. Where it's absent (browser/OBS),
// the page hides that UI and otherwise runs unchanged.

const { app, BrowserWindow, Menu, ipcMain, dialog, screen } = require('electron')
const path = require('path')
const http = require('http')

const { ServerProcess } = require('./server-process')
const { OutputManager } = require('./output-manager')
const { JsonStore } = require('./store')

const ADMIN_WINDOW = { width: 1400, height: 900, minWidth: 900, minHeight: 600 }

// --- Command-line switches (must be set before app is ready) -----------------

// Linux rendering backend. Default is Chromium's X11 backend (XWayland on a
// Wayland session): the most reliable path for placing fullscreen output windows
// on a *specific* physical monitor, because the compositor lets us position by
// pixel bounds — which the Screens feature relies on. Opt into native Wayland
// (lower latency, but the compositor owns placement) with SEVENTHSLIDE_OZONE=wayland,
// or force X11 with SEVENTHSLIDE_OZONE=x11.
const _ozone = (process.env.SEVENTHSLIDE_OZONE || '').toLowerCase()
if (_ozone === 'wayland') {
  app.commandLine.appendSwitch('ozone-platform-hint', 'auto')
  app.commandLine.appendSwitch('enable-features', 'WaylandWindowDecorations')
} else if (_ozone === 'x11') {
  app.commandLine.appendSwitch('ozone-platform', 'x11')
}

// GPU acceleration. Chromium ships a conservative GPU blocklist that can silently
// drop a driver to software rendering — the root cause of the scroll stutter this
// app fought for a long time. Forcing hardware rasterization on (and ignoring the
// blocklist) keeps rasterization/compositing on the GPU. Because Electron owns its
// HWNDs it also presents through DirectComposition on Windows, the efficient path
// an embedded webview cannot use. Verify at chrome://gpu
// (Ctrl+Shift+G) that "Rasterization: Hardware accelerated".
app.commandLine.appendSwitch('ignore-gpu-blocklist')
app.commandLine.appendSwitch('enable-gpu-rasterization')
app.commandLine.appendSwitch('enable-zero-copy')

let server = null
let outputManager = null
let store = null
let adminWindow = null
let quitting = false
const diagWindows = [] // chrome://gpu windows, held so they aren't garbage-collected

// ---------------------------------------------------------------------------
// Single-instance lock — a second launch just focuses the existing window. Two
// backends fighting over the same data dir / database (and the fixed port) would
// be a real bug.
// ---------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (adminWindow) {
      if (adminWindow.isMinimized()) adminWindow.restore()
      adminWindow.focus()
    }
  })
  app.whenReady().then(main).catch(fatal)
}

async function main() {
  // No native menu bar — this is a kiosk-style control surface, not a document app.
  // Removes the default File/Edit/View/Window menu from every window. (Standard
  // text-editing shortcuts — copy/paste/cut/select-all/undo in inputs — keep
  // working: Chromium handles those natively, independent of the app menu.)
  Menu.setApplicationMenu(null)

  store = new JsonStore(app.getPath('userData'), 'desktop-state.json', {})

  server = new ServerProcess({
    isPackaged: app.isPackaged,
    appRoot: path.join(__dirname, '..'),
    resourcesPath: process.resourcesPath,
    logDir: path.join(app.getPath('userData'), 'logs'),
  })
  server.on('crash', onServerCrash)
  server.on('error', (err) => fatal(err))

  try {
    await server.start()
  } catch (err) {
    return fatal(err)
  }

  outputManager = new OutputManager({
    urlFor: (p) => server.url(p),
    store,
    onChange: () => sendToAdmin('outputs:changed'),
  })

  // Keep the renderer's screen picker live as monitors come and go.
  for (const evt of ['display-added', 'display-removed', 'display-metrics-changed']) {
    screen.on(evt, () => sendToAdmin('displays:changed'))
  }

  registerIpc()
  createAdminWindow()

  // Restore last session's screen assignments once we know which outputs exist.
  try {
    const outputs = await fetchOutputNames()
    outputManager.restore(outputs)
  } catch (err) {
    console.error('[main] Could not restore screen assignments:', err.message)
  }
}

function createAdminWindow() {
  adminWindow = new BrowserWindow({
    ...ADMIN_WINDOW,
    title: 'SeventhSlide',
    backgroundColor: '#1e1e1e',
    // Window/taskbar icon at runtime. The packaged installer/app icon comes from
    // electron-builder (see package.json build.*.icon); this covers the dev run
    // (`npm start`) and the Linux taskbar. The PNG is bundled with the app files.
    icon: path.join(__dirname, '..', 'icons', 'seventhslide-icon.png'),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  adminWindow.once('ready-to-show', () => adminWindow.show())
  adminWindow.loadURL(server.url('/admin'))

  // Ctrl+Shift+G opens chrome://gpu in its own window — confirm acceleration.
  adminWindow.webContents.on('before-input-event', (e, input) => {
    if (input.type === 'keyDown' && input.control && input.shift &&
        String(input.key).toLowerCase() === 'g') {
      e.preventDefault()
      openDiagnostics()
    }
  })

  // Closing the control window means the operator is done — shut the whole app
  // (and therefore every output window and the backend) down.
  adminWindow.on('closed', () => {
    adminWindow = null
    if (!quitting) app.quit()
  })
}

function openDiagnostics() {
  const w = new BrowserWindow({ width: 1000, height: 820, title: 'SeventhSlide — GPU status (chrome://gpu)' })
  w.loadURL('chrome://gpu')
  diagWindows.push(w)
  w.on('closed', () => {
    const i = diagWindows.indexOf(w)
    if (i >= 0) diagWindows.splice(i, 1)
  })
}

// ---------------------------------------------------------------------------
// IPC — the entire surface the admin page can reach. Every handler is defensive
// about argument shape because it crosses a trust boundary (a network-loaded page).
// ---------------------------------------------------------------------------
function registerIpc() {
  ipcMain.handle('displays:list', () => outputManager.listDisplays())
  ipcMain.handle('outputs:listOpen', () => outputManager.listOpen())
  ipcMain.handle('outputs:listMuted', () => outputManager.listMuted())

  ipcMain.handle('output:open', (_e, args) => {
    const name = String((args && args.outputName) || '')
    const displayId = Number(args && args.displayId)
    if (!name || !Number.isFinite(displayId)) throw new Error('open: invalid arguments')
    return outputManager.openOutput(name, displayId)
  })

  ipcMain.handle('output:close', (_e, args) => {
    const name = String((args && args.outputName) || '')
    if (!name) throw new Error('close: invalid arguments')
    outputManager.closeOutput(name)
    return true
  })

  ipcMain.handle('outputs:closeAll', () => {
    outputManager.closeAll()
    return true
  })

  ipcMain.handle('output:setMuted', (_e, args) => {
    const name = String((args && args.outputName) || '')
    if (!name) throw new Error('setMuted: invalid arguments')
    return outputManager.setOutputMuted(name, !!(args && args.muted))
  })
}

function sendToAdmin(channel, payload) {
  if (adminWindow && !adminWindow.isDestroyed()) {
    adminWindow.webContents.send(channel, payload)
  }
}

/** Pull the list of configured output names from the running server. */
function fetchOutputNames() {
  return new Promise((resolve, reject) => {
    http
      .get(server.url('/api/state'), (res) => {
        let body = ''
        res.on('data', (c) => (body += c))
        res.on('end', () => {
          try {
            const state = JSON.parse(body)
            resolve((state.outputs || []).map((o) => o.name))
          } catch (err) {
            reject(err)
          }
        })
      })
      .on('error', reject)
  })
}

// ---------------------------------------------------------------------------
// Failure & shutdown
// ---------------------------------------------------------------------------
function onServerCrash({ code, signal }) {
  if (quitting) return
  const logHint = server && server.logPath ? `\n\nServer log:\n${server.logPath}` : ''
  dialog.showErrorBox(
    'SeventhSlide server stopped',
    `The presentation server exited unexpectedly (code=${code}, signal=${signal}).${logHint}`
  )
  app.quit()
}

function fatal(err) {
  console.error('[main] fatal:', err)
  const logHint = server && server.logPath ? `\n\nServer log:\n${server.logPath}` : ''
  dialog.showErrorBox(
    'SeventhSlide failed to start',
    `${err && err.message ? err.message : err}${logHint}`
  )
  app.quit()
}

app.on('before-quit', () => {
  quitting = true
  // shutdown() (not closeAll()) tears down output windows WITHOUT clearing the
  // saved screen assignments, so they reopen on the same monitors next launch.
  if (outputManager) outputManager.shutdown()
  if (server) server.stop()
})

// The app is a control surface for a server, so quitting when the control window
// is gone is correct (including on macOS).
app.on('window-all-closed', () => app.quit())
