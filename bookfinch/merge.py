"""Duplicate merging, relevance, filters and sorting (pure logic)."""
import re

from .models import FORMAT_ORDER, STOP, lang_ok, norm

FILTERS = {"Flexible": "flex", "Strict": "strict", "No filter": "none"}
AUDIO_MARKERS = {"audiobook", "audiolibro"}  # title words that mark an audio edition (English / Spanish)
SORT_LABELS = {"Relevance": "rel", "Title": "title", "Author": "author", "Year": "year",
               "Language": "lang", "No. of formats": "fmt", "Sources": "src"}


# ---------------- Cached normalisation (computed once per book) ----------------
def _prep(it):
    """Compute and store the book's comparison keys (norm() is expensive: do not repeat it)."""
    d = it.__dict__
    if d.get("_k") is None:
        tl, al = norm(it.title).split(), norm(it.author).split()
        d["_tl"], d["_al"] = tl, al
        d["_k"] = " ".join(tl)[:60]
        d["_t"] = {w for w in tl if w not in STOP}
        d["_a"] = {w for w in al if len(w) >= 4}
    return it


def _reprep(it):
    it.__dict__["_k"] = None
    return _prep(it)


# ---------------- Merging ----------------
def author_tokens(a):
    return {t for t in norm(a).split() if len(t) >= 4}


def _compat(t1, t2, strict=False):
    if not t1 or not t2:
        return not strict
    return bool(t1 & t2)


def authors_compatible(a1, a2, strict=False):
    """Non-strict: an unknown author matches anyone. Strict: both known and sharing at least one name."""
    return _compat(author_tokens(a1), author_tokens(a2), strict)


def title_tokens(t):
    return {w for w in norm(t).split() if w not in STOP}


def _same_book(a, b):
    _prep(a), _prep(b)
    ta, tb = a._t, b._t
    if bool(ta & AUDIO_MARKERS) != bool(tb & AUDIO_MARKERS):
        return False  # an audiobook is never just another "format" of the text
    if a._k == b._k:
        return _compat(a._a, b._a)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # "Moby Dick" ~ "Moby Dick; or, The Whale"
    return len(small) >= 2 and small <= big and _compat(a._a, b._a, strict=True)


class Store:
    """Set of books found so far; merges duplicates while keeping every format.
    Uses title and word indexes so a new book is compared only with candidates (before: with all)."""

    def __init__(self):
        self.items = []
        self.by_key, self.by_tok = {}, {}

    def clear(self):
        self.items.clear()
        self.by_key.clear()
        self.by_tok.clear()

    def merge(self, new):
        _prep(new)
        cands = list(self.by_key.get(new._k, ()))
        seen = {id(c) for c in cands}
        for t in new._t:  # a "contained" title necessarily shares some word
            for c in self.by_tok.get(t, ()):
                if id(c) not in seen:
                    seen.add(id(c))
                    cands.append(c)
        for it in cands:
            if _same_book(it, new):
                self._absorb(it, new)
                return it, False
        self.items.append(new)
        self.by_key.setdefault(new._k, []).append(new)
        for t in new._t:
            self.by_tok.setdefault(t, []).append(new)
        return new, True

    @staticmethod
    def _absorb(it, new):
        for s in new.sources:
            if s not in it.sources:
                it.sources.append(s)
        seen = {f["url"] for f in it.files}
        it.files += [f for f in new.files if f["url"] not in seen]
        if new.resolvers:
            it.resolvers += new.resolvers
            if it.rstate == "done":
                it.rstate = "idle"
        if it.year == "—":
            it.year = new.year
        if not it.author and new.author:
            it.author = new.author
            _reprep(it)  # the author changed: refresh its keys (the title is unchanged, so the indexes stay valid)
        if not it.lang and new.lang:
            it.lang = new.lang


def sorted_files(files):
    rank = lambda f: (FORMAT_ORDER.index(f["ext"]) if f["ext"] in FORMAT_ORDER else 99, f["src"])
    seen, out = set(), []
    for f in sorted(files, key=rank):
        if f["url"] not in seen:
            seen.add(f["url"])
            out.append(f)
    return out


# ---------------- Relevance ----------------
def query_words(q):
    return [w for w in norm(q).split() if w not in STOP]


def _has_all(ws, toks):
    return all(any(t.startswith(w) for t in toks) for w in ws)


def matched(it, ws):
    """How many of the searched words appear in the title or author."""
    _prep(it)
    toks = it._tl + it._al
    return sum(1 for w in ws if any(t.startswith(w) for t in toks))


def relevance(it, ws, L=None):
    """Higher = better. Each matched word adds 10; bonus if all are in the author (+30), all in the
    title (+20) or spread across both (+10); +5 if the book's language is the chosen one.
    0 = no word matches."""
    if not ws:
        return 1
    m = matched(it, ws)
    if m == 0:
        return 0
    score = m * 10
    if _has_all(ws, it._al):
        score += 30
    elif _has_all(ws, it._tl):
        score += 20
    elif m == len(ws):
        score += 10
    if L and it.lang and lang_ok(it.lang, L):
        score += 5
    return score


def passes(it, ws, mode):
    if mode == "none" or not ws:
        return True
    m = matched(it, ws)
    return m == len(ws) if mode == "strict" else m >= 1


# ---------------- Sorting ----------------
def _sort_key(it, col):
    if col == "rel":
        return -it.rel
    if col == "title":
        return " ".join(_prep(it)._tl)
    if col == "author":
        return " ".join(_prep(it)._al) or None
    if col == "year":
        return int(it.year) if it.year.isdigit() else None
    if col == "lang":
        return it.lang or None
    if col == "fmt":
        if it.resolvers and not it.files:
            return None  # formats not fetched yet: not a real "0"
        return len({f["ext"] for f in it.files})
    return norm(", ".join(it.sources))


def order_items(items, col, desc=False):
    """Alphabetical order (numeric for year/formats); unknown values always go last."""
    known = [i for i in items if _sort_key(i, col) is not None]
    unknown = [i for i in items if _sort_key(i, col) is None]
    known.sort(key=lambda i: (_sort_key(i, col), _prep(i)._k), reverse=desc)
    return known + unknown


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip().rstrip(". ")[:90].rstrip(". ")
    if not s:
        return "book"
    if s.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
                                   *("LPT%d" % i for i in range(1, 10))}:
        s = "_" + s  # reserved Windows names
    return s
