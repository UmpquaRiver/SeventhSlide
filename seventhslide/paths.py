"""Path resolution, app data dir, and logging setup (foundation layer)."""
import os
import sys
import logging
from functools import lru_cache



# Suppress noisy "ConnectionClosedError exception in shielded future" tracebacks
# that occur when the OS suspends and WebSocket keepalive pings time out.
# Clients reconnect automatically via their onclose handlers.
logging.getLogger('websockets').setLevel(logging.CRITICAL)

# App logger with its own handler: uvicorn does not configure the root logger, and
# INFO would otherwise be dropped before start_server() runs. Scoped so uvicorn
# output is unchanged and warnings are not double-printed.
logger = logging.getLogger('seventhslide')
logger.setLevel(logging.INFO)
if not logger.handlers:  # re-import safe; never stack duplicate handlers
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logger.addHandler(_handler)


# ---------------------- Path Resolution ----------------------

APP_NAME = 'SeventhSlide'


def get_base_dir():
    """Install/program directory (frozen executable dir, else project root).

    Bundled read-only assets only — writable data belongs in get_data_dir().
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # seventhslide/ is one level below the project root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _platform_data_dir():
    """OS-standard per-user application-data directory for this app."""
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
    """Writable user-data directory (DB, exports, uploads, caches).

    Defaults: ``%APPDATA%\\SeventhSlide`` (Windows),
    ``~/Library/Application Support/SeventhSlide`` (macOS),
    ``$XDG_DATA_HOME/SeventhSlide`` or ``~/.local/share/SeventhSlide`` (Linux).
    Override with ``SEVENTHSLIDE_DATA_DIR``.
    """
    override = os.environ.get('SEVENTHSLIDE_DATA_DIR')
    data_dir = os.path.abspath(os.path.expanduser(override)) if override else _platform_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    logger.info("Data directory: %s", data_dir)
    return data_dir


def get_resource_path(relative_path):
    """Path to a bundled read-only resource (PyInstaller ``_MEIPASS`` when frozen)."""
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
