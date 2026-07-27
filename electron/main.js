'use strict'

// SeventhSlide Electron shell: single instance, start backend, open /admin,
// IPC for fullscreen outputs, restore screen assignments, clean shutdown.
// Browser/OBS clients use the same backend without `window.seventhslide`.

const { app, BrowserWindow, Menu, ipcMain, dialog, screen } = require('electron')
const path = require('path')
const http = require('http')

const { ServerProcess } = require('./server-process')
const { OutputManager } = require('./output-manager')
const { JsonStore } = require('./store')

const ADMIN_WINDOW = { width: 1400, height: 900, minWidth: 900, minHeight: 600 }

// --- Command-line switches (must be set before app is ready) -----------------

// Linux: default X11/XWayland for reliable per-monitor fullscreen placement.
// SEVENTHSLIDE_OZONE=wayland | x11 to override.
const _ozone = (process.env.SEVENTHSLIDE_OZONE || '').toLowerCase()
if (_ozone === 'wayland') {
  app.commandLine.appendSwitch('ozone-platform-hint', 'auto')
  app.commandLine.appendSwitch('enable-features', 'WaylandWindowDecorations')
} else if (_ozone === 'x11') {
  app.commandLine.appendSwitch('ozone-platform', 'x11')
}

// Force GPU rasterization (Chromium blocklist can otherwise cause scroll stutter).
// Confirm at chrome://gpu (Ctrl+Shift+G).
app.commandLine.appendSwitch('ignore-gpu-blocklist')
app.commandLine.appendSwitch('enable-gpu-rasterization')
app.commandLine.appendSwitch('enable-zero-copy')

let server = null
let outputManager = null
let store = null
let adminWindow = null
let quitting = false
const diagWindows = [] // chrome://gpu windows, held so they aren't garbage-collected

// Single-instance lock — a second launch focuses the existing window.
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
  // Kiosk-style control surface: no native app menu (edit shortcuts still work).
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
    // Runtime window icon (installer icons come from package.json build config).
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

  // Closing the control window quits the whole app (outputs + backend).
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

// IPC surface for the admin page. Validate args — the page is network-loaded.
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
  // shutdown() keeps persisted assignments; closeAll() would clear them.
  if (outputManager) outputManager.shutdown()
  if (server) server.stop()
})

app.on('window-all-closed', () => app.quit())
