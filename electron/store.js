'use strict'

// Tiny, dependency-free JSON store persisted in Electron's userData directory.
//
// This holds *machine-local desktop-shell* state only — specifically which output
// is sent to which physical monitor. That mapping is intentionally NOT stored in
// the application's songs.db: display identifiers are meaningless on a different
// computer, and the database is meant to stay portable. Keeping screen assignments
// here keeps the two concerns cleanly separated.

const fs = require('fs')
const path = require('path')

class JsonStore {
  /**
   * @param {string} dir   Directory to store the file in (e.g. app.getPath('userData')).
   * @param {string} name  File name.
   * @param {object} defaults  Default object returned when the file is absent/corrupt.
   */
  constructor(dir, name, defaults = {}) {
    this.path = path.join(dir, name)
    this.defaults = defaults
    this.data = this._load()
  }

  _load() {
    try {
      const raw = fs.readFileSync(this.path, 'utf-8')
      const parsed = JSON.parse(raw)
      // Shallow-merge over defaults so newly-added keys always exist.
      return { ...this.defaults, ...parsed }
    } catch (err) {
      if (err.code !== 'ENOENT') {
        console.error(`[store] Failed to read ${this.path}, using defaults:`, err.message)
      }
      return { ...this.defaults }
    }
  }

  get(key, fallback = undefined) {
    return key in this.data ? this.data[key] : fallback
  }

  set(key, value) {
    this.data[key] = value
    this._save()
  }

  _save() {
    try {
      // Atomic write: write to a temp file then rename, so a crash mid-write can
      // never leave a half-written (corrupt) config behind.
      const tmp = `${this.path}.tmp`
      fs.mkdirSync(path.dirname(this.path), { recursive: true })
      fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2), 'utf-8')
      fs.renameSync(tmp, this.path)
    } catch (err) {
      console.error(`[store] Failed to write ${this.path}:`, err.message)
    }
  }
}

module.exports = { JsonStore }
