"""Network: JSON requests with retries, caching and cancellation; safe downloads."""
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from .models import safe_url
from .settings import get_logger

log = get_logger()
TIMEOUT = 15
BACKOFF = (1.0, 2.5)        # waits between retries on HTTP 429/5xx
CACHE_TTL = 300             # seconds a response is reused (avoids hammering the free APIs)
CACHE_MAX = 300
STATE = {"google_key": "", "contact": "", "europeana_key": "", "core_key": ""}

_local = threading.local()
_cache, _cache_lock = {}, threading.Lock()


class Cancelled(Exception):
    pass


def user_agent():
    c = STATE["contact"].strip()
    return "Bookfinch/%s (free book search%s)" % (__version__, "; " + c if c else "")


def bind_cancel(event):
    """Bind a cancellation threading.Event to the current thread."""
    _local.cancel = event


def current_cancel():
    return getattr(_local, "cancel", None)


def _cancelled():
    ev = current_cancel()
    return bool(ev and ev.is_set())


def _wait(seconds):
    ev = current_cancel()
    if ev:
        ev.wait(seconds)
    else:
        time.sleep(seconds)
    if _cancelled():
        raise Cancelled()


def describe_error(e):
    if isinstance(e, Cancelled):
        return "cancelled"
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP error %s" % e.code
    if isinstance(e, ValueError):
        return str(e)
    if isinstance(e, (OSError, TimeoutError)):
        return "no connection or timed out"
    return type(e).__name__


def get_json(url, headers=None, use_cache=True):
    return json.loads(get_bytes(url, headers, use_cache).decode("utf-8", "replace"))


def get_text(url, headers=None, use_cache=True):
    return get_bytes(url, headers, use_cache).decode("utf-8", "replace")


def get_bytes(url, headers=None, use_cache=True):
    if not safe_url(url):
        raise ValueError("URL not allowed: %r" % url)
    key = (url, tuple(sorted((headers or {}).items())))
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    hdrs = {"User-Agent": user_agent(), "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    attempt = 0
    while True:
        if _cancelled():
            raise Cancelled()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=TIMEOUT) as r:
                data = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    data = gzip.decompress(data)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < len(BACKOFF):
                delay = BACKOFF[attempt]
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.strip().isdigit():
                    delay = min(max(delay, int(ra)), 8)
                attempt += 1
                _wait(delay)
                continue
            raise
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 4]:
                del _cache[k]
        _cache[key] = (time.time(), data)
    return data


def check_head(ext, chunk):
    head = chunk.lstrip()[:20].lower()
    if ext != "html" and head.startswith((b"<!doctype html", b"<html")):
        raise ValueError("The server returned a web page instead of the book")
    if ext in ("epub", "zip") and chunk[:2] != b"PK":
        raise ValueError("The file is not a valid EPUB/ZIP")
    if ext == "pdf" and b"%PDF" not in chunk[:1024]:
        raise ValueError("The file is not a valid PDF")
    if ext == "djvu" and not chunk.startswith(b"AT&T"):
        raise ValueError("The file is not a valid DjVu")


def download(url, path, ext, progress=None, cancel=None):
    """Download to `path` (via a .part file). Raises Cancelled if `cancel` is set, ValueError if the
    content is not what was expected or arrives incomplete."""
    if not safe_url(url):
        raise ValueError("URL not allowed")
    tmp = path + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            total, done, first = int(r.headers.get("Content-Length") or 0), 0, True
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(65536)
                if not chunk:
                    break
                if first:
                    first = False
                    check_head(ext, chunk)
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if first:
            raise ValueError("Empty download")
        if total and done != total:
            raise ValueError("Incomplete download (%d of %d bytes)" % (done, total))
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
