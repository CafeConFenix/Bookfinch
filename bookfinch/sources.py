"""Book sources: parsers (pure, testable offline) and the queries sent to each API.

Every query function has the form  fn(q, L, loans, page=0, wide=False):
  page  -> 0 = first batch; 1, 2... = "More results" (the next pages of each API)
  wide  -> extended search: more variants of the text (no accents, no stop words, single keywords)
"""
import functools
import json
import re
import threading
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

from . import net
from .models import LANGS, MIME_EXT, STOP, ext_of, isbn_of, lang_ok, make_item, norm, words
from .settings import get_logger

log = get_logger()
ATOM = "{http://www.w3.org/2005/Atom}"
DCTERMS = "{http://purl.org/dc/terms/}"


# ---------------- Utilities ----------------
def qlist(q, wide=False):
    """Variants of the search text, without repeats. Normal: the original (+ accent-free if different).
    Extended: also without stop words, plus the longest keywords on their own."""
    q = " ".join((q or "").replace('"', " ").split())
    plain = norm(q)
    out = [q]
    if plain and plain != q.lower():
        out.append(plain)
    if wide:
        ws = plain.split()
        keys = [w for w in ws if w not in STOP]
        if keys and len(keys) != len(ws):
            out.append(" ".join(keys))
        if len(keys) >= 2:
            out += [w for w in sorted(keys, key=len, reverse=True)[:3] if len(w) >= 4]
    seen, res = set(), []
    for t in out:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            res.append(t)
    return res


def _xml(text):
    if "<!ENTITY" in text:  # defence against "billion laughs" in hostile XML responses
        raise ValueError("XML not allowed")
    return ET.fromstring(text)


def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _jget(url, headers=None):
    """Like net.get_json, but a 404 (e.g. a page past the end when paginating) counts as "no results"."""
    try:
        return net.get_json(url, headers)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


# ---------------- Parsers ----------------
def parse_openlibrary(data, loans=False):
    out = []
    for d in data.get("docs", []):
        ia, access = d.get("ia") or [], d.get("ebook_access")
        if not d.get("title") or not d.get("key"):
            continue
        page, authors = "https://openlibrary.org" + d["key"], ", ".join(d.get("author_name") or [])
        lang = d.get("language")
        if access == "public" and ia:
            ident = ia[0]
            out.append(make_item(d["title"], authors, d.get("first_publish_year"), "Open Library", page,
                                 resolver=lambda i=ident: archive_files(i, "Open Library"), lang=lang))
        elif loans and access == "borrowable":
            out.append(make_item(d["title"], authors, d.get("first_publish_year"),
                                 "Open Library (loan)", page, lang=lang))
    return out


def parse_gutendex(data, L=None):
    out = []
    for b in data.get("results", []):
        if L is not None and not any(lang_ok(c, L) for c in (b.get("languages") or [])):
            continue
        best = {}  # ext -> (score, url); UTF-8 is preferred over ASCII/ISO-8859 (accents)
        for mime, url in (b.get("formats") or {}).items():
            ext = MIME_EXT.get(mime.split(";")[0].strip())
            if not ext or url.endswith(".zip"):
                continue
            low = mime.lower()
            score = 2 if "utf-8" in low else (0 if ("ascii" in low or "iso-8859" in low) else 1)
            if ext not in best or score > best[ext][0]:
                best[ext] = (score, url.replace("http://www.gutenberg.org", "https://www.gutenberg.org", 1))
        files = [(e, u) for e, (_, u) in best.items()]
        out.append(make_item(b.get("title") or "Untitled",
                             ", ".join(a.get("name", "") for a in b.get("authors") or []),
                             None, "Project Gutenberg",
                             "https://www.gutenberg.org/ebooks/%s" % b.get("id"), files,
                             lang=b.get("languages")))
    return out


def parse_google(data, L=None):
    out = []
    for it in data.get("items", []):
        v, acc = it.get("volumeInfo", {}), it.get("accessInfo", {})
        if not lang_ok(v.get("language"), L, True) or not v.get("title"):
            continue
        page = v.get("canonicalVolumeLink") or v.get("infoLink") or acc.get("webReaderLink")
        if not page:
            continue
        files = [(k, acc[k]["downloadLink"]) for k in ("epub", "pdf")
                 if isinstance(acc.get(k), dict) and acc[k].get("downloadLink")]
        title = v["title"] + ((": " + v["subtitle"]) if v.get("subtitle") else "")
        out.append(make_item(title, ", ".join(v.get("authors") or []),
                             v.get("publishedDate"), "Google Books", page, files, lang=v.get("language")))
    return out


def parse_archive(data, loans=False):
    out = []
    for d in (data.get("response") or {}).get("docs", []):
        ident, title = d.get("identifier"), d.get("title")
        if not ident or not title:
            continue
        if isinstance(title, list):
            title = title[0]
        cr = d.get("creator")
        cr = ", ".join(cr) if isinstance(cr, list) else cr
        page, year, lang = "https://archive.org/details/" + ident, d.get("year") or d.get("date"), d.get("language")
        if str(d.get("access-restricted-item")).lower() == "true":
            if loans:
                out.append(make_item(title, cr, year, "Internet Archive (loan)", page, lang=lang))
            continue
        out.append(make_item(title, cr, year, "Internet Archive", page,
                             resolver=lambda i=ident: archive_files(i, "Internet Archive"), lang=lang))
    return out


def parse_archive_metadata(data, ident, src):
    best = {}
    for f in data.get("files", []):
        name = f.get("name", "")
        ext = ext_of(name)
        if not ext or f.get("private") == "true":
            continue
        fmt = f.get("format", "")
        score = 2 if ("Text PDF" in fmt or fmt == "EPUB" or fmt == "Kindle") else 1
        if ext not in best or score > best[ext][0]:
            best[ext] = (score, "https://archive.org/download/%s/%s" % (
                urllib.parse.quote(ident), urllib.parse.quote(name)))
    return [{"ext": e, "url": u, "src": src} for e, (_, u) in best.items()]


def parse_wikisource(data, L=None):
    out = []
    lg = (L or {"a2": "es"})["a2"]
    for s in (data.get("query") or {}).get("search", []):
        t = s.get("title", "")
        if not t or "/" in t:
            continue
        q = urllib.parse.quote(t)
        page = "https://%s.wikisource.org/wiki/%s" % (lg, urllib.parse.quote(t.replace(" ", "_")))
        files = [(f, "https://ws-export.wmcloud.org/?format=%s&lang=%s&page=%s" % (f, lg, q))
                 for f in ("epub", "pdf")]
        out.append(make_item(t, "", None, "Wikisource", page, files, lang=lg))
    return out


def parse_zenodo(data, L=None):
    out = []
    for h in (data.get("hits") or {}).get("hits", []):
        m = h.get("metadata", {})
        if not lang_ok(m.get("language"), L, True) or not m.get("title"):  # many records carry no language
            continue
        files, seen = [], set()
        for f in h.get("files") or []:
            ext = ext_of(f.get("key", ""))
            url = (f.get("links") or {}).get("self") or \
                "https://zenodo.org/records/%s/files/%s" % (h.get("id"), urllib.parse.quote(f.get("key", "")))
            if ext and ext not in seen:
                seen.add(ext)
                files.append((ext, url))
        page = (h.get("links") or {}).get("self_html") or "https://zenodo.org/records/%s" % h.get("id")
        out.append(make_item(m["title"], ", ".join(c.get("name", "") for c in m.get("creators") or []),
                             m.get("publication_date"), "Zenodo", page, files, lang=m.get("language")))
    return out


def parse_openalex(data):
    out = []
    for w in data.get("results", []):
        title = w.get("display_name") or w.get("title")
        loc = w.get("best_oa_location") or {}
        page = loc.get("landing_page_url") or (w.get("open_access") or {}).get("oa_url") or w.get("id")
        if not title or not page:
            continue
        files = [("pdf", loc["pdf_url"])] if loc.get("pdf_url") else []
        authors = ", ".join((a.get("author") or {}).get("display_name", "")
                            for a in w.get("authorships") or [])
        out.append(make_item(title, authors, w.get("publication_year"), "OpenAlex", page, files,
                             lang=w.get("language")))
    return out


def parse_dspace(items, base, src, L=None):
    out = []
    for it in items if isinstance(items, list) else []:
        md = {}
        for m in it.get("metadata") or []:
            md.setdefault(m.get("key"), []).append(m.get("value") or "")
        title = (md.get("dc.title") or [it.get("name")])[0]
        if not title:
            continue
        langs = md.get("dc.language") or []
        if L and langs and not any(lang_ok(x, L) for x in langs):
            continue
        authors = md.get("dc.contributor.author") or md.get("dc.contributor.editor") or \
            md.get("dc.contributor") or []
        year = ((md.get("dc.date.issued") or md.get("dc.date.available") or [""])[0])[:4]
        uris = [u for u in md.get("dc.identifier.uri") or [] if u.startswith("http")]
        page = uris[0] if uris else ("%s/handle/%s" % (base, it["handle"]) if it.get("handle") else None)
        if not page:
            continue
        files, seen = [], set()
        for bs in it.get("bitstreams") or []:
            mt = (bs.get("mimeType") or "").lower()
            ext = "pdf" if "pdf" in mt else "epub" if "epub" in mt else None
            link = bs.get("retrieveLink")
            if ext and link and ext not in seen:
                seen.add(ext)
                files.append((ext, base + (link if link.startswith("/rest") else "/rest" + link)))
        out.append(make_item(title, ", ".join(authors), year, src, page, files, lang=langs))
    return out


def parse_librivox(data, L=None):
    out = []
    for b in (data or {}).get("books", []):
        if L and not lang_ok(b.get("language"), L):
            continue
        if not b.get("title"):
            continue
        authors = ", ".join(("%s %s" % (a.get("first_name", ""), a.get("last_name", ""))).strip()
                            for a in b.get("authors") or [])
        files = [("zip", b["url_zip_file"])] if b.get("url_zip_file") else []
        out.append(make_item(b["title"] + " (audiobook)", authors, b.get("copyright_year"), "LibriVox",
                             b.get("url_librivox") or b.get("url_iarchive") or "https://librivox.org", files,
                             lang=b.get("language")))
    return out


# ---- Newer sources ----
def parse_wolnelektury(data):
    """Wolne Lektury (Polish literature): the list gives title/author; formats are fetched when the book is selected."""
    out = []
    for b in data if isinstance(data, list) else []:
        title, href = b.get("title"), b.get("href")
        if not title or not href:
            continue
        slug = b.get("slug") or href.rstrip("/").rsplit("/", 1)[-1]
        page = b.get("url") or "https://wolnelektury.pl/katalog/lektura/%s/" % slug
        out.append(make_item(title, b.get("author"), None, "Wolne Lektury", page, lang="pl",
                             resolver=lambda h=href: wl_files(h)))
    return out


def parse_wl_detail(d, src="Wolne Lektury"):
    files = []
    for ext in ("epub", "pdf", "mobi", "fb2", "txt", "html"):
        url = d.get(ext) or (d.get("formats") or {}).get(ext)
        if isinstance(url, str) and url:
            files.append({"ext": ext, "url": url, "src": src})
    return files


def parse_commons(data):
    """Wikimedia Commons: scanned books (PDF and DjVu) uploaded as files."""
    pages = (data.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    out = []
    for p in sorted(pages, key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        ext = {"application/pdf": "pdf", "image/vnd.djvu": "djvu", "image/x-djvu": "djvu"}.get(ii.get("mime"))
        title = (p.get("title") or "").replace("File:", "", 1)
        if not ext or not ii.get("url") or not title:
            continue
        title = title.rsplit(".", 1)[0].replace("_", " ").strip()
        page = ii.get("descriptionurl") or "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(p["title"])
        out.append(make_item(title, "", None, "Wikimedia Commons", page, [(ext, ii["url"])]))
    return out


def parse_gallica(xml_text, L=None):
    """Gallica (BnF): SRU response with Dublin Core records."""
    out = []
    for rec in _xml(xml_text).iter():
        if _ln(rec.tag) != "dc":
            continue
        f = {}
        for ch in rec:
            f.setdefault(_ln(ch.tag), []).append((ch.text or "").strip())
        ark = next((i for i in f.get("identifier", []) if "gallica.bnf.fr/ark:" in i), None)
        title = " ".join((f.get("title") or [""])[0].split()).split(" / ")[0].strip()
        if not ark or not title:
            continue
        langs = f.get("language", [])
        if L and langs and not any(lang_ok(x, L) for x in langs):
            continue
        author = (f.get("creator") or [""])[0].split(". ")[0]
        year = next((d for d in f.get("date", []) if d[:4].isdigit()), "")
        has_pdf = any("application/pdf" in x for x in f.get("format", []))
        out.append(make_item(title, author, year, "Gallica (BnF)", ark,
                             [("pdf", ark.rstrip("/") + ".pdf")] if has_pdf else [], lang=langs))
    return out


def parse_loc(data):
    """Library of Congress (Selected Digitized Books): formats are fetched when the book is selected."""
    out = []
    for r in data.get("results") or []:
        url = (r.get("url") or r.get("id") or "").replace("http://", "https://", 1)
        title = r.get("title")
        if not url or not title or "/item/" not in url:
            continue
        contrib = r.get("contributor") or r.get("contributors") or []
        author = ", ".join(contrib[:2]) if isinstance(contrib, list) else str(contrib)
        out.append(make_item(title if isinstance(title, str) else str(title), author, r.get("date"),
                             "Library of Congress", url, lang=r.get("language"),
                             resolver=lambda u=url: loc_files(u)))
    return out


def parse_loc_item(data, src="Library of Congress"):
    files, seen = [], set()

    def add(ext, url):
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            files.append({"ext": ext, "url": url.replace("http://", "https://", 1), "src": src})
    for res in data.get("resources") or []:
        if isinstance(res, dict):
            add("pdf", res.get("pdf"))
            for group in res.get("files") or []:
                for f in group if isinstance(group, list) else [group]:
                    if not isinstance(f, dict):
                        continue
                    mt = f.get("mimetype")
                    if mt == "application/pdf":
                        add("pdf", f.get("url"))
                    elif mt == "application/epub+zip":
                        add("epub", f.get("url"))
                    elif mt == "text/plain":
                        add("txt", f.get("fulltext_service") or f.get("url"))
    return files


def parse_opds(xml_text, base, src, L=None):
    """OPDS catalogue (Atom): Unglue.it and compatible ones."""
    out = []
    for e in _xml(xml_text).findall(ATOM + "entry"):
        title = " ".join((e.findtext(ATOM + "title") or "").split())
        if not title:
            continue
        files, page = [], ""
        for l in e.findall(ATOM + "link"):
            href = urllib.parse.urljoin(base, l.get("href") or "")
            rel, typ = l.get("rel") or "", (l.get("type") or "").split(";")[0].strip()
            if "acquisition" in rel and typ in MIME_EXT:
                files.append((MIME_EXT[typ], href))
            elif rel == "alternate" and not page and typ.startswith("text/html"):
                page = href
        lang = e.findtext(DCTERMS + "language") or e.findtext("{http://purl.org/dc/elements/1.1/}language") or ""
        if L and lang and not lang_ok(lang, L):
            continue
        author = e.findtext(ATOM + "author/" + ATOM + "name") or ""
        year = (e.findtext(DCTERMS + "issued") or e.findtext(ATOM + "published") or "")[:4]
        if not page and not files:
            continue
        out.append(make_item(title, author, year, src, page or files[0][1], files, lang=lang))
    return out


# ---- Spanish and multilingual sources ----
# dc.type values that look like books (includes Spanish terms: Spanish repositories use them)
BOOKISH = re.compile(r"book|libro|monogra|cap[ií]tulo|chapter|ebook|manual|tratado", re.I)


def _first(v, default=""):
    if isinstance(v, (list, tuple)):
        return next((x for x in v if x), default)
    return v or default


def parse_europeana(data, L=None):
    """Europeana (aggregates Hispana, the BNE, the Biblioteca Virtual Miguel de Cervantes and hundreds of libraries)."""
    out = []
    for it in data.get("items") or []:
        title = str(_first(it.get("title"))).strip()
        if not title or it.get("type") not in (None, "TEXT"):
            continue
        langs = it.get("language") or []
        if L and langs and not any(lang_ok(x, L) for x in langs):
            continue
        by, at = _first(it.get("edmIsShownBy")), _first(it.get("edmIsShownAt"))
        page = it.get("guid") or at or by
        if not page:
            continue
        ext = ext_of(by) if by else None
        files = [(ext, by)] if ext in ("pdf", "epub", "mobi", "djvu", "txt") else []
        out.append(make_item(title, ", ".join(it.get("dcCreator") or [])[:90], _first(it.get("year")),
                             "Europeana", page, files, lang=langs))
    return out


def parse_wikibooks(data, lg):
    """Wikibooks: free textbooks (page only, no direct download)."""
    out = []
    for s in (data.get("query") or {}).get("search", []):
        t = s.get("title", "")
        if not t or "/" in t:
            continue
        out.append(make_item(t, "", None, "Wikibooks", "https://%s.wikibooks.org/wiki/%s" % (
            lg["a2"], urllib.parse.quote(t.replace(" ", "_"))), lang=lg["a2"]))
    return out


def parse_dspace7(data, name, ui_base, api_base, L=None):
    """DSpace 7 repositories (Digital.CSIC, e-Archivo UC3M...): only records that look like books."""
    objs = (((data.get("_embedded") or {}).get("searchResult") or {}).get("_embedded") or {}).get("objects") or []
    out = []
    for o in objs:
        it = (o.get("_embedded") or {}).get("indexableObject") or {}
        md = {k: [x.get("value", "") for x in v if isinstance(x, dict)]
              for k, v in (it.get("metadata") or {}).items() if isinstance(v, list)}
        title, uuid = _first(md.get("dc.title")) or it.get("name"), it.get("uuid")
        if not title or not uuid:
            continue
        types = " ".join(md.get("dc.type", []))
        if types and not BOOKISH.search(types):
            continue  # articles, theses, datasets...
        langs = md.get("dc.language.iso") or md.get("dc.language") or []
        if L and langs and not any(lang_ok(x, L) for x in langs):
            continue
        uris = [u for u in md.get("dc.identifier.uri", []) if u.startswith("http")]
        page = uris[0] if uris else ("%s/handle/%s" % (ui_base, it["handle"]) if it.get("handle")
                                     else "%s/items/%s" % (ui_base, uuid))
        authors = md.get("dc.contributor.author") or md.get("dc.creator") or md.get("dc.contributor.editor") or []
        year = _first(md.get("dc.date.issued"))[:4]
        out.append(make_item(title, ", ".join(authors), year, name, page, lang=langs,
                             resolver=lambda u=uuid, a=api_base, n=name: dspace7_files(a, u, n)))
    return out


def parse_dspace7_item(data, src):
    """Bitstreams of an item's ORIGINAL bundle (fetched with ?embed=bundles/bitstreams)."""
    bundles = (((data.get("_embedded") or {}).get("bundles") or {}).get("_embedded") or {}).get("bundles") or []
    files, seen = [], set()
    for b in bundles:
        if b.get("name") != "ORIGINAL":
            continue
        for bs in (((b.get("_embedded") or {}).get("bitstreams") or {}).get("_embedded") or {}).get("bitstreams") or []:
            ext = ext_of(bs.get("name", ""))
            href = ((bs.get("_links") or {}).get("content") or {}).get("href")
            if ext and href and ext not in seen:
                seen.add(ext)
                files.append({"ext": ext, "url": href, "src": src})
    return files


def parse_core(data, L=None):
    """CORE (aggregates university repositories; requires a free key)."""
    out = []
    for w in data.get("results") or []:
        title = w.get("title")
        if not title:
            continue
        lang = (w.get("language") or {}).get("code") if isinstance(w.get("language"), dict) else w.get("language")
        if L and lang and not lang_ok(lang, L):
            continue
        dl = w.get("downloadUrl") or ""
        files = [(ext_of(dl) or "pdf", dl)] if dl else []
        authors = ", ".join(a.get("name", "") for a in (w.get("authors") or []) if isinstance(a, dict))
        out.append(make_item(title, authors, w.get("yearPublished"), "CORE",
                             "https://core.ac.uk/works/%s" % w.get("id"), files, lang=lang))
    return out


def parse_opds2(data, base, src, L=None):
    """OPDS 2.0 catalogue (JSON): Feedbooks and compatible ones."""
    pubs = list(data.get("publications") or [])
    for g in data.get("groups") or []:
        pubs += g.get("publications") or []
    out = []
    for p in pubs:
        md = p.get("metadata") or {}
        title = md.get("title")
        title = " ".join(str(_first(list(title.values())) if isinstance(title, dict) else title or "").split())
        if not title:
            continue
        lang = _first(md.get("language"))
        if L and lang and not lang_ok(lang, L):
            continue
        au = md.get("author") or []
        au = au if isinstance(au, list) else [au]
        author = ", ".join((a.get("name") if isinstance(a, dict) else str(a)) or "" for a in au[:2])
        files, page = [], ""
        for l in p.get("links") or []:
            href = urllib.parse.urljoin(base, l.get("href") or "")
            rel, typ = l.get("rel") or "", (l.get("type") or "").split(";")[0].strip()
            rel = " ".join(rel) if isinstance(rel, list) else rel
            if "acquisition" in rel and typ in MIME_EXT:
                files.append((MIME_EXT[typ], href))
            elif typ.startswith("text/html") and not page:
                page = href
        if not page and not files:
            continue
        out.append(make_item(title, author, str(md.get("published") or "")[:4], src, page or files[0][1],
                             files, lang=lang))
    return out


def opds_next(body, base):
    """The "next page" link of an OPDS 1 (Atom) or 2 (JSON) feed, or ''."""
    try:
        if body.lstrip().startswith("{"):
            for l in json.loads(body).get("links") or []:
                rel = l.get("rel")
                if "next" in (rel if isinstance(rel, list) else [rel]):
                    return urllib.parse.urljoin(base, l.get("href") or "")
        else:
            for l in _xml(body).findall(ATOM + "link"):
                if l.get("rel") == "next":
                    return urllib.parse.urljoin(base, l.get("href") or "")
    except (ValueError, ET.ParseError):
        pass
    return ""


def parse_opds_any(body, base, src, L=None):
    if body.lstrip().startswith("{"):
        return parse_opds2(json.loads(body), base, src, L)
    return parse_opds(body, base, src, L)


def expand_search(tmpl, q):
    """Fill in the search template of an OPDS/OpenSearch catalogue ({searchTerms} or {?query})."""
    enc = urllib.parse.quote(q, safe="")
    url = re.sub(r"\{searchTerms\??\}", enc, tmpl)

    def form(m):
        names = [n.strip().rstrip("*") for n in m.group(1).split(",") if n.strip()]
        first = next((n for n in names if n in ("query", "q", "search", "searchTerms", "keywords")),
                     names[0] if names else "q")
        return "?%s=%s" % (first, enc)
    url = re.sub(r"\{\?([^}]*)\}", form, url)
    url = re.sub(r"\{[^}]*\}", "", url)  # optional parameters without a value
    sp = urllib.parse.urlsplit(url)
    pairs = [kv for kv in sp.query.split("&") if kv and not kv.endswith("=")]
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "&".join(pairs), ""))


_OPDS_SEARCH = {}


def opds_search_template(root):
    """Discover (and remember) the search template of an OPDS catalogue, starting from its root."""
    if root in _OPDS_SEARCH:
        return _OPDS_SEARCH[root]
    text, tmpl = net.get_text(root, {"Accept": "application/atom+xml, application/opds+json, */*"}), ""
    if text.lstrip().startswith("{"):
        for l in json.loads(text).get("links") or []:
            rel = l.get("rel")
            if "search" in (rel if isinstance(rel, list) else [rel]) and l.get("href"):
                tmpl = urllib.parse.urljoin(root, l["href"])
                break
    else:
        for l in _xml(text).findall(ATOM + "link"):
            if l.get("rel") != "search" or not l.get("href"):
                continue
            href = urllib.parse.urljoin(root, l.get("href"))
            if "opensearchdescription" in (l.get("type") or ""):
                for u in _xml(net.get_text(href)).iter():
                    if _ln(u.tag) == "Url" and u.get("template") and "html" not in (u.get("type") or ""):
                        tmpl = urllib.parse.urljoin(href, u.get("template"))
                        break
            else:
                tmpl = href
            if tmpl:
                break
    if tmpl:
        _OPDS_SEARCH[root] = tmpl
    return tmpl


def dspace7_files(api_base, uuid, src):
    return parse_dspace7_item(net.get_json("%s/core/items/%s?embed=bundles/bitstreams"
                                           % (api_base, urllib.parse.quote(uuid))), src)


# ---------------- Network queries ----------------
def archive_files(ident, src):
    return parse_archive_metadata(net.get_json("https://archive.org/metadata/" + urllib.parse.quote(ident)),
                                  ident, src)


def wl_files(href):
    return parse_wl_detail(net.get_json(href))


def loc_files(item_url):
    return parse_loc_item(net.get_json(item_url.rstrip("/") + "/?fo=json&at=resources"))


def _variants(fn, variants):
    """Run the search variants IN PARALLEL and combine whatever answers; fails only if all of them fail."""
    cancel = net.current_cancel()
    results, errors = [None] * len(variants), []

    def run(i, v):
        net.bind_cancel(cancel)
        try:
            results[i] = fn(v)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i, v), daemon=True) for i, v in enumerate(variants)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = [x for r in results if r for x in r]
    if not out and errors:
        raise errors[0]
    return out


def _no_isbn(fn):
    """Sources without ISBN search: return nothing instead of searching a number as plain text."""
    @functools.wraps(fn)
    def wrapper(q, L=None, loans=False, page=0, wide=False):
        return [] if isbn_of(q) else fn(q, L, loans, page, wide)
    return wrapper


def q_openlibrary(q, L=None, loans=False, page=0, wide=False):
    isbn = isbn_of(q)

    def one(v):
        kind, text = v
        p = {"limit": 100, "page": page + 1, "has_fulltext": "true",
             "fields": "key,title,author_name,first_publish_year,ebook_access,ia,language", kind: text}
        if L:
            p["language"] = L["a3"]
        return parse_openlibrary(
            net.get_json("https://openlibrary.org/search.json?" + urllib.parse.urlencode(p)), loans)
    if isbn:
        return _variants(one, [("isbn", isbn)])
    texts = qlist(q, wide)
    return _variants(one, [("q", t) for t in texts] + [("author", texts[0])])


@_no_isbn
def q_gutendex(q, L=None, loans=False, page=0, wide=False):
    def one(v):
        key, text, pg = v
        p = {key: text, "page": pg}
        if L:
            p["languages"] = L["a2"]
        return parse_gutendex(_jget("https://gutendex.com/books/?" + urllib.parse.urlencode(p)), L)
    texts, pages = qlist(q, wide), (2 * page + 1, 2 * page + 2)  # 32 books per page
    vs = [("search", texts[0], pg) for pg in pages] + [("search", t, pages[0]) for t in texts[1:]]
    if page == 0 or wide:
        vs.append(("topic", texts[0], pages[0]))  # also by topic/bookshelf
    return _variants(one, vs)


def q_google(q, L=None, loans=False, page=0, wide=False):
    isbn = isbn_of(q)

    def one(v):
        expr, start = v
        p = {"q": expr, "filter": "free-ebooks", "maxResults": 40, "startIndex": start}
        if L:
            p["langRestrict"] = L["a2"]
        if net.STATE["google_key"].strip():
            p["key"] = net.STATE["google_key"].strip()
        return parse_google(_jget("https://www.googleapis.com/books/v1/volumes?"
                                  + urllib.parse.urlencode(p)), L)
    if isbn:
        return _variants(one, [("isbn:" + isbn, 0)])
    texts, base = qlist(q, wide), 80 * page
    vs = [(texts[0], base), (texts[0], base + 40)] + [(t, base) for t in texts[1:]]
    if page == 0:
        vs += [('inauthor:"%s"' % texts[0], 0), ('intitle:"%s"' % texts[0], 0)]
    return _variants(one, vs)


def _archive_exprs(text, wide):
    w = words(text)
    if not w:
        return []
    exprs = [" AND ".join("(title:%s OR creator:%s)" % (x, x) for x in w)]
    if wide and len(w) > 1:  # any of the words (relevance sorts them afterwards)
        exprs.append("(" + " OR ".join("title:%s OR creator:%s" % (x, x) for x in w) + ")")
    return exprs


def q_archive(q, L=None, loans=False, page=0, wide=False):
    isbn = isbn_of(q)

    def one(expr):
        e = expr + " AND mediatype:texts"
        if not loans:
            e += " AND -access-restricted-item:true"
        if L:
            e += " AND language:(%s)" % " OR ".join([L["a3"], L["a2"]] + L["names"])
        params = [("q", e), ("rows", 100), ("page", page + 1), ("output", "json"), ("sort[]", "downloads desc")] + \
                 [("fl[]", f) for f in ("identifier", "title", "creator", "year", "language",
                                        "access-restricted-item")]
        return parse_archive(net.get_json("https://archive.org/advancedsearch.php?"
                                          + urllib.parse.urlencode(params)), loans)
    exprs = ["isbn:" + isbn] if isbn else [x for t in qlist(q, wide) for x in _archive_exprs(t, wide)]
    return _variants(one, list(dict.fromkeys(exprs))) if exprs else []


def _q_dspace(base, src, q, L, page, wide):
    def one(text):
        url = base + "/rest/search?" + urllib.parse.urlencode(
            {"query": text, "expand": "metadata,bitstreams", "limit": 100, "offset": 100 * page})
        return parse_dspace(net.get_json(url, {"Accept": "application/json"}), base, src, L)
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_doab(q, L=None, loans=False, page=0, wide=False):
    return _q_dspace("https://directory.doabooks.org", "DOAB", q, L, page, wide)


@_no_isbn
def q_oapen(q, L=None, loans=False, page=0, wide=False):
    return _q_dspace("https://library.oapen.org", "OAPEN", q, L, page, wide)


@_no_isbn
def q_librivox(q, L=None, loans=False, page=0, wide=False):
    base = "https://librivox.org/api/feed/audiobooks/?"
    w = words(q)
    variants = [("title", t) for t in qlist(q, wide)] + ([("author", w[-1])] if w else [])

    def one(v):
        try:
            return parse_librivox(net.get_json(base + urllib.parse.urlencode(
                {v[0]: v[1], "format": "json", "limit": 50, "offset": 50 * page})), L)
        except Exception as e:  # LibriVox answers with an HTTP error when nothing matches
            if getattr(e, "code", None) in (400, 404):
                return []
            raise
    return _variants(one, variants)


@_no_isbn
def q_wikisource(q, L=None, loans=False, page=0, wide=False):
    # Wikisource is one wiki per language: with "all languages" the main ones are queried
    langs = [L] if L else [LANGS[k] for k in ("English", "Spanish", "French", "German", "Italian", "Portuguese")]

    def one(v):
        lg, text = v
        return parse_wikisource(net.get_json("https://%s.wikisource.org/w/api.php?" % lg["a2"]
                                             + urllib.parse.urlencode({
                                                 "action": "query", "list": "search", "srsearch": text,
                                                 "srnamespace": 0, "srlimit": 50, "sroffset": 50 * page,
                                                 "format": "json"})), lg)
    return _variants(one, [(lg, t) for lg in langs for t in qlist(q, wide)])


@_no_isbn
def q_zenodo(q, L=None, loans=False, page=0, wide=False):
    def one(text):
        return parse_zenodo(net.get_json("https://zenodo.org/api/records?" + urllib.parse.urlencode({
            "q": text, "type": "publication", "subtype": "book", "access_right": "open",
            "size": 100, "page": page + 1})), L)
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_openalex(q, L=None, loans=False, page=0, wide=False):
    flt = "type:book,open_access.is_oa:true" + (",language:" + L["a2"] if L else "")

    def one(text):
        return parse_openalex(net.get_json("https://api.openalex.org/works?" + urllib.parse.urlencode({
            "search": text, "filter": flt, "per-page": 100, "page": page + 1})))
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_wolnelektury(q, L=None, loans=False, page=0, wide=False):
    if L is not None and L["a2"] != "pl":
        return []  # Polish literature catalogue

    def one(text):
        return parse_wolnelektury(net.get_json("https://wolnelektury.pl/api/books/?" + urllib.parse.urlencode(
            {"search": text, "page": page + 1})))
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_commons(q, L=None, loans=False, page=0, wide=False):
    def one(text):
        return parse_commons(net.get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrnamespace": 6, "gsrsearch": "filetype:office " + text, "gsrlimit": 50,
            "gsroffset": 50 * page, "prop": "imageinfo", "iiprop": "url|mime"})))
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_gallica(q, L=None, loans=False, page=0, wide=False):
    def one(text):
        cql = '(dc.title all "%s" or dc.creator all "%s") and dc.type all "monographie"' % (text, text)
        if L:
            cql += ' and dc.language all "%s"' % L["a3"]
        url = "https://gallica.bnf.fr/SRU?" + urllib.parse.urlencode({
            "version": "1.2", "operation": "searchRetrieve", "query": cql,
            "maximumRecords": 50, "startRecord": 1 + 50 * page})
        return parse_gallica(net.get_text(url), L)
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_loc(q, L=None, loans=False, page=0, wide=False):
    def one(text):
        fa = "access-restricted:false" + ("|language:" + L["names"][0].lower() if L else "")
        return parse_loc(net.get_json("https://www.loc.gov/collections/selected-digitized-books/?"
                                      + urllib.parse.urlencode({"q": text, "fo": "json", "c": 100,
                                                                "sp": page + 1, "at": "results,pagination",
                                                                "fa": fa})))
    return _variants(one, qlist(q, wide))


@_no_isbn
def q_unglue(q, L=None, loans=False, page=0, wide=False):
    if page > 0:
        return []  # the search feed does not paginate

    def one(text):
        url = "https://unglue.it/api/opds/s.%s/" % urllib.parse.quote(text, safe="")
        return parse_opds(net.get_text(url), url, "Unglue.it", L)
    return _variants(one, qlist(q, wide)[:3])


def _q_opds(root, src, q, L, page, wide):
    def one(text):
        tmpl = opds_search_template(root)
        if not tmpl:
            raise ValueError("the catalogue does not advertise a search endpoint")
        url = expand_search(tmpl, text)
        body = net.get_text(url)
        for _ in range(page):  # "more results": follow the "next" links
            nxt = opds_next(body, url)
            if not nxt:
                return []
            url, body = nxt, net.get_text(nxt)
        return parse_opds_any(body, url, src, L)
    return _variants(one, qlist(q, wide)[:3])


@_no_isbn
def q_textos(q, L=None, loans=False, page=0, wide=False):
    if L is not None and L["a2"] != "es":
        return []  # catalogue entirely in Spanish
    return _q_opds("https://www.textos.info/catalogo.atom", "textos.info", q, L, page, wide)


@_no_isbn
def q_feedbooks(q, L=None, loans=False, page=0, wide=False):
    return _q_opds("https://catalog.feedbooks.com/catalog/index.json", "Feedbooks", q, L, page, wide)


@_no_isbn
def q_europeana(q, L=None, loans=False, page=0, wide=False):
    key = net.STATE["europeana_key"].strip() or "api2demo"  # public demo key

    def one(text):
        t = text.replace('"', " ")
        params = [("wskey", key), ("query", "(title:(%s) OR who:(%s))" % (t, t)), ("qf", "TYPE:TEXT"),
                  ("reusability", "open"), ("rows", 100), ("start", 1 + 100 * page), ("profile", "standard")]
        if L:
            params.append(("qf", "LANGUAGE:" + L["a2"]))
        return parse_europeana(net.get_json("https://api.europeana.eu/record/v2/search.json?"
                                            + urllib.parse.urlencode(params)), L)
    return _variants(one, qlist(q, wide)[:3])


@_no_isbn
def q_wikibooks(q, L=None, loans=False, page=0, wide=False):
    langs = [L] if L else [LANGS[k] for k in ("English", "Spanish", "French", "German")]

    def one(v):
        lg, text = v
        return parse_wikibooks(net.get_json("https://%s.wikibooks.org/w/api.php?" % lg["a2"]
                                            + urllib.parse.urlencode({
                                                "action": "query", "list": "search", "srsearch": text,
                                                "srnamespace": 0, "srlimit": 50, "sroffset": 50 * page,
                                                "format": "json"})), lg)
    return _variants(one, [(lg, t) for lg in langs for t in qlist(q, wide)[:2]])


# (name, web interface, DSpace 7 REST API)
DSPACE7 = [("Digital.CSIC", "https://digital.csic.es", "https://digital.csic.es/server/api"),
           ("e-Archivo UC3M", "https://e-archivo.uc3m.es", "https://e-archivo.uc3m.es/server/api")]


@_no_isbn
def q_dspace7(q, L=None, loans=False, page=0, wide=False):
    def one(v):
        (name, ui, api), text = v
        return parse_dspace7(net.get_json(api + "/discover/search/objects?" + urllib.parse.urlencode({
            "query": text, "dsoType": "item", "size": 50, "page": page})), name, ui, api, L)
    return _variants(one, [(r, t) for r in DSPACE7 for t in qlist(q, wide)[:2]])


@_no_isbn
def q_core(q, L=None, loans=False, page=0, wide=False):
    key = net.STATE["core_key"].strip()
    if not key:
        return []  # optional: enabled by pasting a free key in Settings

    def one(text):
        return parse_core(net.get_json("https://api.core.ac.uk/v3/search/works?" + urllib.parse.urlencode({
            "q": text, "limit": 50, "offset": 50 * page}), {"Authorization": "Bearer " + key}), L)
    return _variants(one, qlist(q, wide)[:2])


SOURCES = [("Open Library", q_openlibrary), ("Project Gutenberg", q_gutendex),
           ("Google Books", q_google), ("Internet Archive", q_archive),
           ("Wikisource", q_wikisource), ("Zenodo", q_zenodo), ("OpenAlex", q_openalex),
           ("DOAB", q_doab), ("OAPEN", q_oapen), ("LibriVox", q_librivox),
           ("Wikimedia Commons", q_commons), ("Library of Congress", q_loc),
           ("Gallica (BnF)", q_gallica), ("Unglue.it", q_unglue), ("Wolne Lektury", q_wolnelektury),
           ("Europeana", q_europeana), ("textos.info", q_textos), ("Feedbooks", q_feedbooks),
           ("Wikibooks", q_wikibooks), ("DSpace repositories", q_dspace7), ("CORE", q_core)]


def start_search(q, L, loans, wave, out_queue, sources=None, page=0, wide=False):
    """Start a search on daemon threads (one per source). Each source puts into `out_queue`
    ("res", wave, name, items) or ("err", wave, name, exception). Returns the Event used to cancel."""
    cancel = threading.Event()

    def work(name, fn):
        net.bind_cancel(cancel)
        try:
            out_queue.put(("res", wave, name, fn(q, L, loans, page=page, wide=wide)))
        except Exception as e:
            if not isinstance(e, net.Cancelled):
                log.warning("%s: %s (%r)", name, net.describe_error(e), e)
            out_queue.put(("err", wave, name, e))

    for name, fn in (sources if sources is not None else SOURCES):
        threading.Thread(target=work, args=(name, fn), daemon=True).start()
    return cancel


# Sites without a public API: their own search (or a site-restricted web search) opens in the browser.
def _site(domain):
    return lambda q: "https://duckduckgo.com/?q=" + urllib.parse.quote_plus("%s site:%s" % (q, domain))


# (name, function(q) -> url, needs search text?)
WEB_SITES = [
    ("Amazon Kindle (cheapest first)", lambda q: "https://www.amazon.com/s?" + urllib.parse.urlencode(
        {"k": q, "i": "digital-text", "s": "price-asc-rank"}), True),
    ("Biblioteca Virtual Miguel de Cervantes", lambda q: "https://www.cervantesvirtual.com/buscador/?" +
     urllib.parse.urlencode({"q": q}), True),
    ("Biblioteca Digital Hispánica (BNE)", _site("bdh.bne.es"), True),
    ("Memoria Chilena", _site("memoriachilena.gob.cl"), True),
    ("Europeana", lambda q: "https://www.europeana.eu/en/search?" + urllib.parse.urlencode({"query": q}), True),
    ("HathiTrust", lambda q: "https://catalog.hathitrust.org/Search/Home?" + urllib.parse.urlencode(
        {"lookfor": q, "type": "all"}), True),
    ("Standard Ebooks", lambda q: "https://standardebooks.org/ebooks?" + urllib.parse.urlencode({"query": q}), True),
    ("Smashwords (free indie ebooks)", lambda q: "https://www.smashwords.com/books/search?" +
     urllib.parse.urlencode({"query": q}), True),
    ("ManyBooks", _site("manybooks.net"), True),
    ("SciELO Libros", _site("books.scielo.org"), True),
    ("Wikibooks", _site("wikibooks.org"), True),
    # --- Spanish-language libraries and portals without a public API (site-restricted search) ---
    ("Elejandría (classics in PDF/EPUB/MOBI)", _site("elejandria.com"), True),
    ("Biblioteca Virtual Universal (Argentina)", _site("biblioteca.org.ar"), True),
    ("Hispana (digital libraries of Spain)", _site("hispana.mcu.es"), True),
    ("Libroteca", _site("libroteca.net"), True),
    ("Freeditorial", _site("freeditorial.com"), True),
    ("El Libro Total", _site("ellibrototal.com"), True),
    ("Alibrate", _site("alibrate.com"), True),
    ("Lektu (freely licensed books)", _site("lektu.com"), True),
    ("InfoLibros", _site("infolibros.org"), True),
    ("Libros CSIC (open access)", _site("editorial.csic.es"), True),
    ("CLACSO – Virtual Library", _site("biblioteca-repositorio.clacso.edu.ar"), True),
    ("Biblioteca Nacional Digital de Chile", _site("bibliotecanacionaldigital.gob.cl"), True),
    ("Dialnet (articles and books)", _site("dialnet.unirioja.es"), True),
    ("Redalyc", _site("redalyc.org"), True),
    ("eBiblio – loans with a library card (Spain)", lambda q: "https://www.ebiblio.es", False),
]
