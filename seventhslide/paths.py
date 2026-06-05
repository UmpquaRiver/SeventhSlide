"""Path resolution, app data dir, and logging setup (foundation layer)."""
import os
import sys
import logging
from functools import lru_cache



# Suppress noisy "ConnectionClosedError exception in shielded future" tracebacks
# that occur when the OS suspends and WebSocket keepalive pings time out.
# Clients reconnect automatically via their onclose handlers.
logging.getLogger('websockets').setLevel(logging.CRITICAL)

# Application logger. Previously the app had essentially no observability — a
# handful of bare prints and ~30 silent `except Exception: pass` blocks. Route
# handlers, swallowed-but-significant failures, and lifecycle events log here so
# misbehavior in a live service leaves a diagnosable trail.
logger = logging.getLogger('seventhslide')


# ---------------------- Path Resolution ----------------------

APP_NAME = 'SeventhSlide'


def get_base_dir():
    """Directory holding the installed program — the executable's folder when frozen
    (PyInstaller), otherwise this script's folder.

    Use this only for locating *bundled, read-only* assets, never for writing user
    data: the install directory is often read-only (Program Files, a macOS .app
    bundle, /opt, ...). Writable data belongs in get_data_dir().
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # This module lives in the `seventhslide/` package one level below the project
    # root (where lyrics.py and the bundled `templates/` dir sit), so step up twice.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _platform_data_dir():
    """The OS-standard, per-user, writable application-data directory for this app."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming')
    elif sys.platform == 'darwin':
        base = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support')
    else:  # Linux, *BSD and other POSIX systems
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, APP_NAME)


def _path_is_within(path, parent):
    """True if `path` is `parent` itself or lives inside it (symlinks resolved)."""
    try:
        path = os.path.realpath(path)
        parent = os.path.realpath(parent)
        return path == parent or os.path.commonpath([path, parent]) == parent
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_data_dir():
    """Absolute path to the writable directory for all user data — the database,
    exported output pages, uploaded images/videos, fonts and caches.

    Resolved once per process and created if missing. This is the OS-standard
    per-user data location (so it works the same whether the program is run from
    source or installed read-only on Windows/macOS/Linux):

        Windows : %APPDATA%\\SeventhSlide
        macOS   : ~/Library/Application Support/SeventhSlide
        Linux   : $XDG_DATA_HOME/SeventhSlide  (default ~/.local/share/SeventhSlide)

    Set SEVENTHSLIDE_DATA_DIR to override the location (useful for tests or a
    portable install).
    """
    override = os.environ.get('SEVENTHSLIDE_DATA_DIR')
    data_dir = os.path.abspath(os.path.expanduser(override)) if override else _platform_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    logger.info("Data directory: %s", data_dir)
    return data_dir


def get_resource_path(relative_path):
    """Absolute path to a bundled, read-only resource (templates, etc.). Handles the
    PyInstaller temp-extraction dir (_MEIPASS) when frozen, the script dir otherwise.
    """
    if getattr(sys, 'frozen', False):
        base_path = getattr(sys, '_MEIPASS', get_base_dir())
    else:
        base_path = get_base_dir()
    return os.path.join(base_path, relative_path)


__all__ = [
    'APP_NAME',
    '_path_is_within',
    '_platform_data_dir',
    'get_base_dir',
    'get_data_dir',
    'get_resource_path',
    'logger',
]
