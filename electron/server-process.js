'use strict'

// Python backend lifecycle: fixed port (SEVENTHSLIDE_PORT), spawn, readiness
// probe, crash signal, clean stop. Plain EventEmitter — Electron stays in main.js.

const { spawn } = require('child_process')
const { EventEmitter } = require('events')
const http = require('http')
const fs = require('fs')
const path = require('path')

const READINESS_TIMEOUT_MS = 30000
const READINESS_POLL_INTERVAL_MS = 200

const DEFAULT_PORT = 49777

/** Port from SEVENTHSLIDE_PORT when valid; otherwise DEFAULT_PORT. */
function resolvePort() {
  const raw = (process.env.SEVENTHSLIDE_PORT || '').trim()
  if (/^\d+$/.test(raw)) {
    const n = Number(raw)
    if (n >= 1 && n <= 65535) return n
  }
  return DEFAULT_PORT
}

class ServerProcess extends EventEmitter {
  /**
   * @param {object} opts
   * @param {boolean} opts.isPackaged   Whether running inside a packaged build.
   * @param {string}  opts.appRoot      Directory containing lyrics.py (dev only).
   * @param {string}  opts.resourcesPath process.resourcesPath (packaged only).
   * @param {string}  opts.logDir       Directory to write server.log into.
   * @param {string}  [opts.python]     Python interpreter to use in dev.
   */
  constructor(opts) {
    super()
    this.opts = opts
    this.proc = null
    this.port = null
    this.ready = false
    this._stopping = false
    this._logStream = null
  }

  _resolveCommand() {
    if (this.opts.isPackaged) {
      const bin = process.platform === 'win32' ? 'lyrics-slideshow.exe' : 'lyrics-slideshow'
      return { cmd: path.join(this.opts.resourcesPath, bin), args: [String(this.port)] }
    }
    const python = this.opts.python ||
      process.env.SEVENTHSLIDE_PYTHON ||
      (process.platform === 'win32' ? 'python' : 'python3')
    return { cmd: python, args: [path.join(this.opts.appRoot, 'lyrics.py'), String(this.port)] }
  }

  _openLog() {
    try {
      fs.mkdirSync(this.opts.logDir, { recursive: true })
      const logPath = path.join(this.opts.logDir, 'server.log')
      // Truncate on each launch so the file reflects the current session and can't
      // grow without bound.
      this._logStream = fs.createWriteStream(logPath, { flags: 'w' })
      this.logPath = logPath
    } catch (err) {
      console.error('[server] Could not open log file:', err.message)
      this._logStream = null
    }
  }

  _writeLog(chunk) {
    process.stdout.write(chunk)
    if (this._logStream) this._logStream.write(chunk)
  }

  /** Start the server. Resolves once it is ready; rejects on spawn/readiness failure. */
  async start() {
    this.port = resolvePort()
    this._openLog()
    const { cmd, args } = this._resolveCommand()
    this._writeLog(`[server] launching: ${cmd} ${args.join(' ')}\n`)

    this.proc = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] })

    this.proc.stdout.on('data', (d) => this._writeLog(d))
    this.proc.stderr.on('data', (d) => this._writeLog(d))

    this.proc.on('error', (err) => {
      this._writeLog(`[server] spawn error: ${err.message}\n`)
      this.emit('error', err)
    })

    this.proc.on('exit', (code, signal) => {
      this._writeLog(`[server] exited (code=${code}, signal=${signal})\n`)
      this.proc = null
      // An exit we didn't ask for, after a clean start, is a crash worth surfacing.
      if (!this._stopping) {
        this.emit('crash', { code, signal })
      }
    })

    await this._waitForReady()
    this.ready = true
    return this.port
  }

  _probe() {
    return new Promise((resolve) => {
      const req = http.get(
        { host: '127.0.0.1', port: this.port, path: '/api/state', timeout: 1500 },
        (res) => {
          // Drain so the socket can be reused/closed promptly.
          res.resume()
          // Require a real success from /api/state — a 404 during a routing
          // mistake must not count the server as ready.
          resolve(res.statusCode === 200)
        }
      )
      req.on('error', () => resolve(false))
      req.on('timeout', () => { req.destroy(); resolve(false) })
    })
  }

  async _waitForReady() {
    const deadline = Date.now() + READINESS_TIMEOUT_MS
    while (Date.now() < deadline) {
      // If the process died during startup, stop waiting immediately.
      if (!this.proc) throw new Error('Server process exited before becoming ready.')
      if (await this._probe()) return
      await new Promise((r) => setTimeout(r, READINESS_POLL_INTERVAL_MS))
    }
    throw new Error(`Server did not become ready within ${READINESS_TIMEOUT_MS / 1000}s.`)
  }

  /** URL helper for the renderer / output windows. */
  url(pathname = '/') {
    return `http://127.0.0.1:${this.port}${pathname}`
  }

  /** Stop the server cleanly. Safe to call multiple times. */
  stop() {
    this._stopping = true
    if (this._logStream) { this._logStream.end(); this._logStream = null }
    if (!this.proc) return
    const proc = this.proc
    this.proc = null
    try {
      if (process.platform === 'win32') {
        // SIGTERM is unreliable for console apps on Windows; kill the tree.
        spawn('taskkill', ['/pid', String(proc.pid), '/f', '/t'])
      } else {
        proc.kill('SIGTERM')
        // Escalate if it ignores SIGTERM.
        setTimeout(() => { try { proc.kill('SIGKILL') } catch (_) {} }, 3000).unref()
      }
    } catch (err) {
      console.error('[server] error during stop:', err.message)
    }
  }
}

module.exports = { ServerProcess, resolvePort, DEFAULT_PORT }
