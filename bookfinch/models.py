"""Data model and text/language helpers (no network, no GUI)."""
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

FORMAT_ORDER = ["epub", "pdf", "mobi", "azw3", "fb2", "txt", "html", "djvu", "zip"]
MIME_EXT = {"application/epub+zip": "epub", "application/x-mobipocket-ebook": "mobi",
            "application/pdf": "pdf", "text/html": "html", "text/plain": "txt",
            "application/x-fictionbook+xml": "fb2"}

LANGS = {  # label shown in the UI -> ISO 639-1 / 639-2 codes and the names Internet Archive uses
    "English": dict(a2="en", a3="eng", names=["English"]),
    "Bulgarian": dict(a2="bg", a3="bul", names=["Bulgarian", "Български"]),
    "Catalan": dict(a2="ca", a3="cat", names=["Catalan", "Català"]),
    "French": dict(a2="fr", a3="fre", names=["French", "Français"]),
    "German": dict(a2="de", a3="ger", names=["German", "Deutsch"]),
    "Italian": dict(a2="it", a3="ita", names=["Italian", "Italiano"]),
    "Polish": dict(a2="pl", a3="pol", names=["Polish", "Polski"]),
    "Portuguese": dict(a2="pt", a3="por", names=["Portuguese", "Português"]),
    "Russian": dict(a2="ru", a3="rus", names=["Russian", "Русский"]),
    "Spanish": dict(a2="es", a3="spa", names=["Spanish", "Español", "Castellano"]),
}
ALL_LANGS = "All languages"
LANG_ALT3 = {"fre": "fra", "ger": "deu"}  # ISO 639-2/T variants
UNKNOWN_YEAR = "—"
# Words ignored when matching a query: Spanish and English articles, prepositions and conjunctions.
STOP = {"de", "la", "el", "los", "las", "del", "y", "e", "en", "a", "al", "lo", "un", "una", "unos", "unas",
        "por", "con", "para", "sin", "sobre", "se", "su", "sus", "que", "o", "u",
        "the", "of", "and", "an", "to", "in", "on", "for"}


def safe_url(url):
    """Only http/https: urllib would also open file:// or ftp:// URLs."""
    if not isinstance(url, str):
        return False
    sp = urllib.parse.urlsplit(url.strip())
    return sp.scheme.lower() in ("http", "https") and bool(sp.netloc)


def lang_ok(code, L, allow_missing=False):
    """Does the resource's language code match the chosen language L (None = all)?
    Accepts es, spa, Spanish, es_ES, es-ES..."""
    if L is None:
        return True
    if not code:
        return allow_missing
    code = str(code).lower().strip()
    base = re.split(r"[-_]", code)[0]
    valid = {L["a2"], L["a3"], LANG_ALT3.get(L["a3"], L["a3"])}
    return code in valid or base in valid or code in [n.lower() for n in L["names"]]


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[\W_]+", " ", s, flags=re.U).strip()


def words(q):
    return [w for w in re.split(r"\s+", re.sub(r"[^\w\s]", " ", q, flags=re.U)) if w][:6]


def isbn_of(q):
    """Return the ISBN (10 or 13 digits, valid checksum) if the query is one; otherwise None."""
    s = re.sub(r"[\s-]", "", q or "")
    if re.fullmatch(r"\d{13}", s):
        total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(s[:12]))
        return s if (10 - total % 10) % 10 == int(s[12]) else None
    if re.fullmatch(r"\d{9}[\dXx]", s):
        total = sum((10 - i) * (10 if c in "Xx" else int(c)) for i, c in enumerate(s))
        return s.upper() if total % 11 == 0 else None
    return None


def ext_of(url):
    m = re.search(r"\.(epub|pdf|mobi|azw3|fb2|djvu|txt|zip)(?:$|[?#])", url or "", re.I)
    return m.group(1).lower() if m else None


@dataclass(eq=False)
class Item:
    title: str
    author: str
    year: str
    sources: list
    page: str
    files: list = field(default_factory=list)       # [{"ext", "url", "src"}]
    resolvers: list = field(default_factory=list)   # functions that fetch more formats (network)
    rel: int = 1
    iid: str = ""
    rstate: str = "idle"                            # idle | loading | failed | done
    lang: str = ""                                  # language code, if the source provides one


def make_item(title, author, year, source, page, files=None, resolver=None, lang=""):
    y = str(year or "")[:4]
    return Item(
        title=title.strip(), author=(author or "").strip()[:90],
        year=y if y.isdigit() else UNKNOWN_YEAR, sources=[source], page=page or "",
        files=[{"ext": e, "url": u, "src": source} for e, u in (files or []) if safe_url(u)],
        resolvers=[resolver] if resolver else [], lang=_lang_code(lang))


def _lang_code(lang):
    """First language code (accepts a list or a string), normalised to 2 letters when known
    (spa/es_ES/Spanish -> es); otherwise the code as given. '' if there is none."""
    if isinstance(lang, (list, tuple)):
        lang = next((x for x in lang if x), "")
    code = re.split(r"[-_]", str(lang or "").strip().lower())[0][:12]
    return _A3_TO_A2.get(code, code)


_A3_TO_A2 = {}
for _l in LANGS.values():
    _A3_TO_A2[_l["a3"]] = _l["a2"]
    _A3_TO_A2[LANG_ALT3.get(_l["a3"], _l["a3"])] = _l["a2"]
    for _n in _l["names"]:
        _A3_TO_A2[_n.lower()] = _l["a2"]
