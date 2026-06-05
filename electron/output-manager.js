'use strict'

// Manages the fullscreen presentation windows that get pushed onto physical
// monitors. One window per output (e.g. "Main", "Stage"), each loading that
// output's page (/<name>.html) from the local server and shown fullscreen on a
// chosen display.
//
// Why a dedicated manager: placing a window on a *specific* physical monitor and
// keeping it there as monitors come and go is the one thing a plain browser can't
// do. Electron's `screen` module gives us real display geometry; we create each
// window at the target display's bounds and go fullscreen, then react to
// hot-plug/unplug events so the operator never ends up with an output stranded on
// a monitor that no longer exists.
//
// Note: Electron's numeric `display.id` is a stable OS handle that crosses IPC via
// structured clone, so — unlike the Qt shell, which had to fold a CRC32 into a
// signed 32-bit int — there is no id-marshalling hazard here; we use it directly.
//
// Two pieces of state, kept deliberately separate:
//   * `assignments` — the *desired* "this output belongs on that monitor" mapping.
//     This is the persisted source of truth. It is changed only by explicit
//     operator intent (Send / Stop / Close-all) — never as a side effect of a
//     window closing because a monitor was unplugged or the app is quitting. That
//     is what lets an output come back automatically when its monitor is replugged
//     or on the next launch.
//   * `windows` — the windows actually open *right now*. Driven by the OS/runtime.

const { BrowserWindow, screen } = require('electron')

const STORE_KEY = 'outputAssignments'
const MUTED_KEY = 'outputMuted'

class OutputManager {
  /**
   * @param {object} opts
   * @param {(pathname: string) => string} opts.urlFor  Builds a server URL for a path.
   * @param {import('./store').JsonStore} opts.store     Persistence for assignments.
   * @param {() => void} opts.onChange   Called whenever the open-output set changes.
   */
  constructor(opts) {
    this.urlFor = opts.urlFor
    this.store = opts.store
    this.onChange = opts.onChange || (() => {})
    this.windows = new Map()      // outputName -> { win, displayId }
    this.assignments = new Map()  // outputName -> display descriptor (desired, persisted)
    // Desired per-output mute state for the LOCAL fullscreen window only. Kept
    // separate from assignments and persisted, so it survives restarts and is
    // applied whenever the window (re)opens.
    this.muted = new Map()        // outputName -> bool
    const savedMuted = this.store.get(MUTED_KEY, {})
    if (savedMuted && typeof savedMuted === 'object') {
      for (const [k, v] of Object.entries(savedMuted)) this.muted.set(k, !!v)
    }
    this._shuttingDown = false
    this._wireScreenEvents()
  }

  // ---- Display enumeration -------------------------------------------------

  /**
   * A display's native pixel resolution. `display.bounds` is in logical
   * (device-independent) pixels, so on a scaled display it understates the real
   * resolution — e.g. a 4K panel at 300% reports 1280x720. Multiplying by the
   * scale factor recovers what the OS display settings show (3840x2160).
   */
  _nativeSize(display) {
    const s = display.scaleFactor || 1
    return { width: Math.round(display.bounds.width * s), height: Math.round(display.bounds.height * s) }
  }

  /** Stable-ish descriptor used to re-match a display across restarts. */
  _descriptor(display) {
    const b = display.bounds
    return {
      id: display.id,
      label: display.label || '',
      bounds: `${b.x},${b.y},${b.width},${b.height}`,
      internal: !!display.internal,
    }
  }

  /** Public list of displays with a friendly label and current (open) assignment. */
  listDisplays() {
    const primaryId = screen.getPrimaryDisplay().id
    const openByDisplay = new Map()
    for (const [name, entry] of this.windows) openByDisplay.set(entry.displayId, name)

    return screen.getAllDisplays().map((d, idx) => {
      const b = d.bounds
      const native = this._nativeSize(d)
      return {
        id: d.id,
        index: idx + 1,
        label: this._friendlyLabel(d, idx, d.id === primaryId),
        // bounds stay in logical pixels (virtual-desktop position/geometry);
        // nativeWidth/Height are the true pixel resolution for display.
        bounds: { x: b.x, y: b.y, width: b.width, height: b.height },
        nativeWidth: native.width,
        nativeHeight: native.height,
        scaleFactor: d.scaleFactor,
        primary: d.id === primaryId,
        internal: !!d.internal,
        assignedOutput: openByDisplay.get(d.id) || null,
        // Short text for the picker button; full identity carried in `detail`.
        shortLabel: this._shortLabel(idx, d.id === primaryId, !!d.internal),
        detail: this._detail(d),
      }
    })
  }

  _friendlyLabel(display, idx, isPrimary) {
    const parts = [`Screen ${idx + 1}`]
    if (display.label) parts.push(display.label)
    if (display.internal) parts.push('built-in')
    else if (isPrimary) parts.push('primary')
    const native = this._nativeSize(display)
    parts.push(`${native.width}×${native.height}`)
    return parts.join(' · ')
  }

  /** Compact label for the picker button — just enough to pick a screen. */
  _shortLabel(idx, isPrimary, internal) {
    let label = `Screen ${idx + 1}`
    if (internal) label += ' · Built-in'
    else if (isPrimary) label += ' · Primary'
    return label
  }

  /** Full, human-readable screen identity: model (or label) · native size. */
  _detail(display) {
    const native = this._nativeSize(display)
    const name = display.label || ''
    const res = `${native.width}×${native.height}`
    return name ? `${name} · ${res}` : res
  }

  /** outputName -> displayId for every currently-open output window. */
  listOpen() {
    const out = {}
    for (const [name, entry] of this.windows) out[name] = entry.displayId
    return out
  }

  /** outputName -> bool: desired mute state for local output windows. */
  listMuted() {
    const out = {}
    for (const [name, m] of this.muted) out[name] = m
    return out
  }

  // ---- Opening / closing output windows ------------------------------------

  _displayById(displayId) {
    return screen.getAllDisplays().find((d) => d.id === displayId) || null
  }

  /**
   * Send an output fullscreen to a display (operator intent). Records the desired
   * assignment, then opens — or relocates, without reloading — the window.
   * Returns the resolved displayId.
   */
  openOutput(outputName, displayId) {
    const display = this._displayById(displayId) || screen.getPrimaryDisplay()

    // Desired assignment is operator intent: record and persist it.
    this.assignments.set(outputName, this._descriptor(display))
    this._persist()

    const existing = this.windows.get(outputName)
    if (existing && !existing.win.isDestroyed()) {
      this._moveToDisplay(existing.win, display)
      existing.displayId = display.id
      this.onChange()
      return display.id
    }

    this._createWindow(outputName, display)
    this.onChange()
    return display.id
  }

  _createWindow(outputName, display) {
    const b = display.bounds
    const win = new BrowserWindow({
      x: b.x,
      y: b.y,
      width: b.width,
      height: b.height,
      fullscreen: true,
      frame: false,
      autoHideMenuBar: true,
      backgroundColor: '#000000',
      title: `SeventhSlide — ${outputName}`,
      show: false,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // Critical for a presentation app: keep rendering at full speed even when
        // the operator's admin window has focus and this window is in the
        // background. Scoped per-window (only outputs opt out) so the admin window
        // is still free to throttle when it isn't the foreground concern.
        backgroundThrottling: false,
      },
    })

    // Output pages are display-only; lock the window down so nothing can navigate
    // it away from our origin or spawn extra windows.
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    win.webContents.on('will-navigate', (e, url) => {
      if (!url.startsWith(this.urlFor('/'))) e.preventDefault()
    })

    // Safety hatch: Escape closes the focused output window even though it's
    // frameless/fullscreen, so an operator is never trapped. This is a real
    // close (operator intent), so it also clears the assignment via closeOutput.
    win.webContents.on('before-input-event', (e, input) => {
      if (input.type === 'keyDown' && input.key === 'Escape') {
        e.preventDefault()
        this.closeOutput(outputName)
      }
    })

    win.webContents.on('did-fail-load', (e, code, desc, url) => {
      if (code === -3) return // ERR_ABORTED — benign (e.g. superseded navigation)
      console.error(`[output:${outputName}] load failed (${code} ${desc}) for ${url}`)
    })

    // The window lifecycle only maintains `windows`; persistence of *desired*
    // assignments is handled at the explicit-intent call sites, never here.
    win.on('closed', () => {
      this.windows.delete(outputName)
      if (!this._shuttingDown) this.onChange()
    })

    win.once('ready-to-show', () => {
      // Re-assert geometry: some compositors place the window before fullscreen
      // is applied. Showing then nudging bounds keeps it on the intended monitor.
      const d = this._displayById(display.id) || display
      this._moveToDisplay(win, d)
      // Apply the saved per-output mute state to this window only — local audio,
      // independent of other clients on the same output page.
      win.webContents.setAudioMuted(this.muted.get(outputName) || false)
      win.show()
    })

    win.loadURL(this.urlFor(`/${encodeURIComponent(outputName)}.html`))
    this.windows.set(outputName, { win, displayId: display.id })
  }

  _moveToDisplay(win, display) {
    const b = display.bounds
    if (win.isFullScreen()) win.setFullScreen(false)
    win.setBounds({ x: b.x, y: b.y, width: b.width, height: b.height })
    win.setFullScreen(true)
  }

  /** Operator explicitly stops an output: forget the assignment and close it. */
  closeOutput(outputName) {
    this.assignments.delete(outputName)
    this._persist()
    const entry = this.windows.get(outputName)
    if (entry && !entry.win.isDestroyed()) {
      entry.win.close()  // 'closed' handler removes it from `windows` and notifies
    } else {
      this.onChange()
    }
  }

  /**
   * Mute/unmute audio on this output's LOCAL fullscreen window only. Records the
   * desired state (persisted, so it survives restarts and applies when the window
   * next opens) and applies it immediately if open. Other clients on the same
   * output page (browsers, OBS) are unaffected.
   */
  setOutputMuted(outputName, muted) {
    muted = !!muted
    this.muted.set(outputName, muted)
    const entry = this.windows.get(outputName)
    if (entry && !entry.win.isDestroyed()) entry.win.webContents.setAudioMuted(muted)
    this._persistMuted()
    this.onChange()
    return muted
  }

  /** Operator clears every output. */
  closeAll() {
    this.assignments.clear()
    this._persist()
    for (const [, entry] of this.windows) {
      if (!entry.win.isDestroyed()) entry.win.destroy()
    }
    this.windows.clear()
    this.onChange()
  }

  /**
   * App is quitting: tear down windows WITHOUT touching the saved assignments, so
   * the same outputs reopen on their monitors next launch.
   */
  shutdown() {
    this._shuttingDown = true
    for (const [, entry] of this.windows) {
      if (!entry.win.isDestroyed()) entry.win.destroy()
    }
    this.windows.clear()
  }

  // ---- Persistence & restore ----------------------------------------------

  _persist() {
    const assignments = []
    for (const [name, descriptor] of this.assignments) {
      assignments.push({ output: name, display: descriptor })
    }
    this.store.set(STORE_KEY, assignments)
  }

  _persistMuted() {
    const obj = {}
    for (const [name, m] of this.muted) obj[name] = m
    this.store.set(MUTED_KEY, obj)
  }

  /** Best-effort match of a saved descriptor to a currently-connected display. */
  _matchDisplay(descriptor) {
    if (!descriptor) return null
    const displays = screen.getAllDisplays()
    return (
      displays.find((d) => d.id === descriptor.id) ||
      displays.find((d) => descriptor.label && d.label === descriptor.label) ||
      displays.find((d) => this._descriptor(d).bounds === descriptor.bounds) ||
      null
    )
  }

  /**
   * Load saved assignments and open the windows whose monitor is present. An
   * assignment to a monitor that isn't connected is kept (so it returns when the
   * monitor is replugged) but simply not opened now.
   * @param {string[]} availableOutputNames  Output names that currently exist on the server.
   */
  restore(availableOutputNames) {
    const saved = this.store.get(STORE_KEY, [])
    if (!Array.isArray(saved)) return
    const valid = new Set(availableOutputNames)

    // Rebuild the desired-assignment map, dropping entries for outputs that no
    // longer exist on the server.
    this.assignments.clear()
    for (const a of saved) {
      if (a && a.output && a.display && valid.has(a.output)) {
        this.assignments.set(a.output, a.display)
      }
    }
    this._persist() // rewrite cleaned set

    // Open the ones whose monitor is currently attached.
    for (const [name, descriptor] of this.assignments) {
      const display = this._matchDisplay(descriptor)
      if (display && !this.windows.has(name)) this._createWindow(name, display)
    }
    this.onChange()
  }

  // ---- Hot-plug handling ---------------------------------------------------

  _wireScreenEvents() {
    screen.on('display-removed', (_e, display) => {
      // Close any output window on the now-gone monitor, but KEEP its assignment
      // so it returns automatically when the monitor is reconnected.
      for (const [name, entry] of this.windows) {
        if (entry.displayId === display.id) {
          if (!entry.win.isDestroyed()) entry.win.destroy()
          this.windows.delete(name)
        }
      }
      this.onChange()
    })

    screen.on('display-added', () => {
      // Reopen any assignment whose monitor just (re)appeared, and re-fit windows.
      for (const [name, descriptor] of this.assignments) {
        if (this.windows.has(name)) continue
        const display = this._matchDisplay(descriptor)
        if (display) this._createWindow(name, display)
      }
      this._refitAll()
      this.onChange()
    })

    screen.on('display-metrics-changed', (_e, display) => {
      for (const [, entry] of this.windows) {
        if (entry.displayId === display.id && !entry.win.isDestroyed()) {
          this._moveToDisplay(entry.win, display)
        }
      }
      this.onChange()
    })
  }

  _refitAll() {
    for (const [, entry] of this.windows) {
      const d = this._displayById(entry.displayId)
      if (d && !entry.win.isDestroyed()) this._moveToDisplay(entry.win, d)
    }
  }
}

module.exports = { OutputManager }
