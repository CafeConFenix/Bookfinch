import sys


def _dpi_aware():
    """Avoid a blurry interface on high-DPI Windows displays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    _dpi_aware()
    import logging
    import os
    from . import APP_NAME
    from .settings import get_logger
    log = get_logger()
    try:
        from .ui import App
        App().mainloop()
        # Window closed and settings saved: exit RIGHT NOW. This stops network threads that are still
        # waiting on a slow server from delaying process exit (especially in the .exe).
        logging.shutdown()
        os._exit(0)
    except Exception as e:  # "windowed" app: there is no console, so the error must end up in the log
        log.exception("fatal error: %r", e)
        try:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Unexpected error:\n%s\n\nSee the log file in the data folder for details." % e)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
