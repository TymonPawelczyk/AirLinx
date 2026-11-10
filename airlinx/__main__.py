"""python -m airlinx [--debug] [--no-window]"""
import sys

# WM_CLASS / app name must be set before Gtk is imported (PyGObject initializes GDK on import).
sys.argv[0] = "airlinx"
from gi.repository import GLib  # noqa: E402

GLib.set_prgname("airlinx")

from .app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
