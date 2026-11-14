"""Settings, data folder (portable mode) and error log."""
import copy
import json
import logging
import os
import sys
import tempfile
import urllib.parse
from logging.handlers import RotatingFileHandler

from .models import safe_url

DEFAULTS = {"sites": [], "lang": "English", "filter": "Flexible", "loans": True,
            "last_dir": "", "history": [], "google_key": "", "contact": "",
            "europeana_key": "", "core_key": ""}
_DATA_DIR = None


def app_dir():
    """Folder that holds the .exe (or the project, when run with Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, ".write-test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir():
    """Portable mode: if `portable.txt` sits next to the program, settings and log are stored in the
    `data` folder beside it. Otherwise (or if that is not writable), in %APPDATA%/Bookfinch."""
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    candidates = []
    if os.path.exists(os.path.join(app_dir(), "portable.txt")):
        candidates.append(os.path.join(app_dir(), "data"))
    candidates.append(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Bookfinch"))
    candidates.append(tempfile.gettempdir())
    for c in candidates:
        if _writable(c):
            _DATA_DIR = c
            return c
    _DATA_DIR = tempfile.gettempdir()
    return _DATA_DIR


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def log_path():
    return os.path.join(data_dir(), "bookfinch.log")


def get_logger():
    lg = logging.getLogger("bookfinch")
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        try:
            h = RotatingFileHandler(log_path(), maxBytes=200_000, backupCount=1, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        except OSError:
            h = logging.NullHandler()
        lg.addHandler(h)
        lg.propagate = False
    return lg


def valid_site_url(url):
    return safe_url(url) and len(url) > 10


def build_url(template, q):
    """Replace {q} with the (URL-encoded) query. Without {q}, the template is opened as is."""
    return template.replace("{q}", urllib.parse.quote_plus(q))


def load_settings():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg
    cfg["sites"] = [{"name": str(x["name"]), "url": str(x["url"])}
                    for x in data.get("sites", [])
                    if isinstance(x, dict) and x.get("name") and valid_site_url(str(x.get("url", "")))]
    for k in ("lang", "filter", "last_dir", "google_key", "contact", "europeana_key", "core_key"):
        if isinstance(data.get(k), str):
            cfg[k] = data[k]
    cfg["loans"] = bool(data.get("loans", True))
    cfg["history"] = [h for h in data.get("history", []) if isinstance(h, str)][:20]
    return cfg


def save_settings(cfg):
    path = settings_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        get_logger().warning("saving settings: %r", e)
        return False
