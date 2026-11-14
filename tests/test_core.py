"""Tests sin dependencias externas:  python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import queue  # noqa: E402
from bookfinch import merge, models, net, settings, sources  # noqa: E402
from bookfinch.models import LANGS, make_item  # noqa: E402

ES = LANGS["Spanish"]


class TextTests(unittest.TestCase):
    def test_lang_variants(self):
        for c in ("es", "spa", "Spanish", "es_ES", "es-ES", "ES"):
            self.assertTrue(models.lang_ok(c, ES), c)
        self.assertFalse(models.lang_ok("en", ES))
        self.assertTrue(models.lang_ok(None, ES, allow_missing=True))
        self.assertFalse(models.lang_ok(None, ES))
        self.assertTrue(models.lang_ok("xx", None))
        self.assertTrue(models.lang_ok("fra", LANGS["French"]))

    def test_isbn(self):
        self.assertEqual(models.isbn_of("978-0-306-40615-7"), "9780306406157")
        self.assertEqual(models.isbn_of("0-306-40615-2"), "0306406152")
        self.assertIsNone(models.isbn_of("9780306406158"))  # bad checksum
        self.assertIsNone(models.isbn_of("don quijote"))
        self.assertIsNone(models.isbn_of("2024"))

    def test_safe_url(self):
        self.assertTrue(models.safe_url("https://a.org/x"))
        for bad in ("file:///etc/passwd", "ftp://a/b", "javascript:alert(1)", "", None, "https://"):
            self.assertFalse(models.safe_url(bad), bad)

    def test_make_item_drops_unsafe_files(self):
        it = make_item("T", "A", "2015-03", "S", "https://p",
                       files=[("pdf", "file:///x.pdf"), ("epub", "https://ok/x.epub")])
        self.assertEqual([f["ext"] for f in it.files], ["epub"])
        self.assertEqual(it.year, "2015")
        self.assertEqual(make_item("T", None, "c19", "S", "p").year, "—")

    def test_safe_name(self):
        self.assertEqual(merge.safe_name('A/B:C*?"<>|'), "A B C")
        self.assertEqual(merge.safe_name("CON"), "_CON")
        self.assertEqual(merge.safe_name("   ..."), "book")


class ParserTests(unittest.TestCase):
    def test_gutendex_prefers_utf8_and_language(self):
        data = {"results": [
            {"id": 1, "title": "Uno", "authors": [{"name": "Autor A"}], "languages": ["es"], "formats": {
                "text/plain; charset=us-ascii": "http://www.gutenberg.org/ascii.txt",
                "text/plain; charset=utf-8": "http://www.gutenberg.org/utf8.txt",
                "application/epub+zip": "http://www.gutenberg.org/1.epub",
                "application/octet-stream": "http://www.gutenberg.org/1.zip"}},
            {"id": 2, "title": "Dos", "authors": [], "languages": ["en"], "formats": {}}]}
        r = sources.parse_gutendex(data, ES)
        self.assertEqual(len(r), 1)
        urls = {f["ext"]: f["url"] for f in r[0].files}
        self.assertEqual(urls["txt"], "https://www.gutenberg.org/utf8.txt")
        self.assertEqual(urls["epub"], "https://www.gutenberg.org/1.epub")
        self.assertEqual(len(sources.parse_gutendex(data, None)), 2)

    def test_openlibrary(self):
        data = {"docs": [
            {"key": "/works/1", "title": "A", "author_name": ["X"], "ebook_access": "public", "ia": ["id1"]},
            {"key": "/works/2", "title": "B", "ebook_access": "borrowable"},
            {"key": "/works/3", "title": "C", "ebook_access": "no_ebook"}]}
        self.assertEqual([i.title for i in sources.parse_openlibrary(data)], ["A"])
        r = sources.parse_openlibrary(data, loans=True)
        self.assertEqual([i.title for i in r], ["A", "B"])
        self.assertTrue(r[0].resolvers and not r[1].resolvers)

    def test_google(self):
        data = {"items": [
            {"volumeInfo": {"title": "T", "subtitle": "S", "language": "es", "authors": ["A"],
                            "publishedDate": "2001-05", "canonicalVolumeLink": "https://g/1"},
             "accessInfo": {"epub": {"downloadLink": "https://g/1.epub"}, "pdf": {"isAvailable": False}}},
            {"volumeInfo": {"title": "X", "language": "en", "infoLink": "https://g/2"}, "accessInfo": {}}]}
        r = sources.parse_google(data, ES)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].title, r[0].year, [f["ext"] for f in r[0].files]), ("T: S", "2001", ["epub"]))

    def test_archive_and_metadata(self):
        data = {"response": {"docs": [
            {"identifier": "a", "title": ["Libro A"], "creator": ["P", "Q"], "year": 1900},
            {"identifier": "b", "title": "Prestado", "access-restricted-item": "true"}]}}
        self.assertEqual(len(sources.parse_archive(data)), 1)
        self.assertEqual(len(sources.parse_archive(data, loans=True)), 2)
        meta = {"files": [{"name": "a.pdf", "format": "Text PDF"}, {"name": "a.epub", "format": "EPUB"},
                          {"name": "x y.pdf", "format": "Image Container PDF"},
                          {"name": "priv.pdf", "private": "true"}, {"name": "a.jpg"}]}
        files = sources.parse_archive_metadata(meta, "a b", "IA")
        self.assertEqual({f["ext"] for f in files}, {"pdf", "epub"})
        self.assertTrue(all("a%20b" in f["url"] for f in files))

    def test_wikisource_has_no_fake_author(self):
        data = {"query": {"search": [{"title": "La Celestina"}, {"title": "Sub/página"}]}}
        r = sources.parse_wikisource(data, ES)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].author, "")

    def test_zenodo_openalex_dspace_librivox(self):
        z = {"hits": {"hits": [{"id": 5, "metadata": {"title": "Z", "language": "spa", "creators": [{"name": "N"}],
                                                      "publication_date": "2020-01-01"},
                                "files": [{"key": "z.pdf", "links": {"self": "https://zenodo.org/f/z.pdf"}}]}]}}
        self.assertEqual(sources.parse_zenodo(z, ES)[0].files[0]["ext"], "pdf")
        oa = {"results": [{"display_name": "OA", "id": "https://openalex.org/W1", "publication_year": 2019,
                           "best_oa_location": {"pdf_url": "https://x/y.pdf", "landing_page_url": "https://x"},
                           "authorships": [{"author": {"display_name": "Ana"}}]}]}
        self.assertEqual(sources.parse_openalex(oa)[0].author, "Ana")
        ds = [{"name": "D", "handle": "1/2", "metadata": [{"key": "dc.title", "value": "Libro D"},
                                                          {"key": "dc.language", "value": "es_ES"},
                                                          {"key": "dc.date.issued", "value": "2018-02"}],
               "bitstreams": [{"mimeType": "application/pdf", "retrieveLink": "/bitstreams/9/retrieve"}]}]
        r = sources.parse_dspace(ds, "https://dspace.org", "DOAB", ES)
        self.assertEqual(len(r), 1)  # es_ES is no longer discarded
        self.assertEqual(r[0].files[0]["url"], "https://dspace.org/rest/bitstreams/9/retrieve")
        lv = {"books": [{"title": "Quijote", "language": "Spanish", "url_zip_file": "https://l/z.zip",
                         "authors": [{"first_name": "Miguel", "last_name": "Cervantes"}]}]}
        self.assertTrue(sources.parse_librivox(lv, ES)[0].title.endswith("(audiobook)"))


class MergeTests(unittest.TestCase):
    def mk(self, t, a, src="A", **kw):
        return make_item(t, a, None, src, "https://p/" + src, **kw)

    def test_subtitle_variants_merge(self):
        st = merge.Store()
        st.merge(self.mk("Don Quijote de la Mancha", "Miguel de Cervantes",
                         files=[("epub", "https://a/1.epub")]))
        it, new = st.merge(self.mk("El ingenioso hidalgo Don Quijote de la Mancha", "Miguel de Cervantes Saavedra",
                                   "B", files=[("pdf", "https://b/1.pdf")]))
        self.assertFalse(new)
        self.assertEqual(len(st.items), 1)
        self.assertEqual({f["ext"] for f in it.files}, {"epub", "pdf"})
        self.assertEqual(it.sources, ["A", "B"])

    def test_no_false_merges(self):
        st = merge.Store()
        st.merge(self.mk("Hamlet", ""))
        st.merge(self.mk("Hamlet Study Guide", "", "B"))                         # no author and short title
        st.merge(self.mk("Poemas", "Pablo Neruda", "C"))
        st.merge(self.mk("Poemas", "Antonio Machado", "D"))                      # same title, different author
        st.merge(self.mk("Don Quijote de la Mancha", "Miguel de Cervantes", "E"))
        st.merge(self.mk("Don Quijote de la Mancha (audiolibro)", "Miguel de Cervantes", "F"))  # Spanish marker
        st.merge(self.mk("Moby Dick", "Herman Melville", "G"))
        st.merge(self.mk("Moby Dick (audiobook)", "Herman Melville", "H"))                      # English marker
        self.assertEqual(len(st.items), 8)

    def test_same_title_unknown_author_merges_and_fills_author(self):
        st = merge.Store()
        st.merge(self.mk("La Celestina", ""))
        it, new = st.merge(self.mk("La Celestina", "Fernando de Rojas", "B"))
        self.assertFalse(new)
        self.assertEqual(it.author, "Fernando de Rojas")

    def test_resolver_added_resets_state(self):
        st = merge.Store()
        a = self.mk("Libro Largo Titulo", "Autor Conocido")
        a.rstate = "done"
        st.merge(a)
        st.merge(self.mk("Libro Largo Titulo", "Autor Conocido", "B", resolver=lambda: []))
        self.assertEqual(a.rstate, "idle")

    def test_relevance_filter_order(self):
        ws = merge.query_words("Cervantes Quijote")
        a = self.mk("Don Quijote", "Miguel de Cervantes")
        b = self.mk("Quijote ilustrado", "Anónimo")
        c = self.mk("Otra cosa", "Nadie")
        a.rel, b.rel, c.rel = (merge.relevance(x, ws) for x in (a, b, c))
        self.assertGreater(a.rel, b.rel)
        self.assertEqual(c.rel, 0)
        self.assertTrue(merge.passes(b, ws, "flex") and not merge.passes(b, ws, "strict"))
        self.assertTrue(merge.passes(c, ws, "none"))
        self.assertEqual([i.title for i in merge.order_items([c, b, a], "rel")][:2], ["Don Quijote", "Quijote ilustrado"])

    def test_unknowns_always_last(self):
        a, b, c = self.mk("B", "Zeta"), self.mk("A", ""), self.mk("C", "Alfa")
        for desc in (False, True):
            self.assertEqual(merge.order_items([a, b, c], "author", desc)[-1], b)
        a.year, b.year, c.year = "1900", "—", "2000"
        self.assertEqual(merge.order_items([a, b, c], "year", True), [c, a, b])

    def test_unresolved_formats_sort_last(self):
        pending = self.mk("P", "X", resolver=lambda: [])
        done = self.mk("D", "X", files=[("pdf", "https://a/1.pdf")])
        self.assertEqual(merge.order_items([pending, done], "fmt", False)[-1], pending)
        self.assertEqual(merge.order_items([pending, done], "fmt", True)[-1], pending)

    def test_sorted_files_dedup_and_order(self):
        fs = [{"ext": "pdf", "url": "u1", "src": "A"}, {"ext": "epub", "url": "u2", "src": "B"},
              {"ext": "pdf", "url": "u1", "src": "C"}]
        self.assertEqual([f["ext"] for f in merge.sorted_files(fs)], ["epub", "pdf"])


class NewSourcesTests(unittest.TestCase):
    def test_qlist(self):
        self.assertEqual(sources.qlist("Cervantes"), ["Cervantes"])
        self.assertEqual(sources.qlist("Poesía Española"), ["Poesía Española", "poesia espanola"])
        wide = sources.qlist("El Ingenioso Hidalgo Don Quijote de la Mancha", wide=True)
        self.assertEqual(wide[0], "El Ingenioso Hidalgo Don Quijote de la Mancha")
        self.assertIn("ingenioso hidalgo don quijote mancha", wide)
        self.assertTrue({"quijote", "hidalgo", "ingenioso"} <= set(wide))
        self.assertEqual(len(wide), len({w.lower() for w in wide}))     # no repeats
        self.assertEqual(sources.qlist('a "b" c'), ["a b c"])           # no quotes

    def test_lang_in_items_and_merge(self):
        it = make_item("T", "A", None, "S", "https://p", lang=["spa", "eng"])
        self.assertEqual(it.lang, "es")
        self.assertEqual(make_item("T", "A", None, "S", "p", lang="es_ES").lang, "es")
        for raw, want in (("fra", "fr"), ("ger", "de"), ("Spanish", "es"), ("pl", "pl"), ("xx", "xx"), (None, ""), ([], "")):
            self.assertEqual(make_item("T", "A", None, "S", "p", lang=raw).lang, want, raw)
        st = merge.Store()
        a = make_item("Libro Largo Titulo", "Autor Conocido", None, "A", "p")
        st.merge(a)
        st.merge(make_item("Libro Largo Titulo", "Autor Conocido", None, "B", "p", lang="fre"))
        self.assertEqual(a.lang, "fr")

    def test_language_bonus(self):
        a = make_item("Don Quijote", "Cervantes", None, "A", "p", lang="spa")
        b = make_item("Don Quijote", "Cervantes", None, "B", "p", lang="eng")
        ws = merge.query_words("don quijote")
        self.assertEqual(merge.relevance(a, ws, ES), merge.relevance(b, ws) + 5)
        self.assertEqual(merge.relevance(b, ws, ES), merge.relevance(b, ws))

    def test_wolne_lektury(self):
        lst = [{"title": "Pan Tadeusz", "author": "Adam Mickiewicz", "slug": "pan-tadeusz",
                "href": "https://wolnelektury.pl/api/books/pan-tadeusz/"}, {"title": "", "href": "x"}]
        r = sources.parse_wolnelektury(lst)
        self.assertEqual((len(r), r[0].lang, r[0].page), (1, "pl", "https://wolnelektury.pl/katalog/lektura/pan-tadeusz/"))
        self.assertTrue(r[0].resolvers)
        det = {"epub": "https://w/p.epub", "pdf": "https://w/p.pdf", "txt": "", "mobi": None,
               "formats": {"fb2": "https://w/p.fb2"}}
        self.assertEqual({f["ext"] for f in sources.parse_wl_detail(det)}, {"epub", "pdf", "fb2"})
        self.assertEqual(sources.parse_wolnelektury({"detail": "x"}), [])

    def test_commons(self):
        pages = [{"title": "File:Don_Quijote_1605.pdf", "index": 2, "imageinfo": [
                      {"url": "https://upload.wikimedia.org/a.pdf", "mime": "application/pdf",
                       "descriptionurl": "https://commons.wikimedia.org/wiki/File:a.pdf"}]},
                 {"title": "File:Foto.jpg", "index": 1, "imageinfo": [{"url": "https://u/f.jpg", "mime": "image/jpeg"}]},
                 {"title": "File:Libro.djvu", "index": 3, "imageinfo": [{"url": "https://u/l.djvu", "mime": "image/vnd.djvu"}]}]
        r = sources.parse_commons({"query": {"pages": pages}})
        self.assertEqual([(i.title, i.files[0]["ext"]) for i in r], [("Don Quijote 1605", "pdf"), ("Libro", "djvu")])
        self.assertEqual(len(sources.parse_commons({"query": {"pages": {"1": pages[0]}}})), 1)  # formatversion 1
        self.assertEqual(sources.parse_commons({}), [])

    GALLICA = """<?xml version="1.0"?><srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
      <srw:records><srw:record><srw:recordData><oai_dc:dc xmlns:dc="http://purl.org/dc/elements/1.1/"
       xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/">
       <dc:creator>Haurigot, Georges. Auteur du texte</dc:creator>
       <dc:format>application/pdf</dc:format><dc:identifier>https://gallica.bnf.fr/ark:/12148/bpt6k9633077k</dc:identifier>
       <dc:language>fre</dc:language><dc:date>1867</dc:date>
       <dc:title>Les malices de Gaspard (10e série) / Par Georges Haurigot ; Illustrations</dc:title></oai_dc:dc>
      </srw:recordData></srw:record>
      <srw:record><srw:recordData><oai_dc:dc xmlns:dc="http://purl.org/dc/elements/1.1/"
       xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"><dc:title>Sin ark</dc:title></oai_dc:dc></srw:recordData></srw:record>
      </srw:records></srw:searchRetrieveResponse>"""

    def test_gallica(self):
        r = sources.parse_gallica(self.GALLICA)
        self.assertEqual(len(r), 1)
        it = r[0]
        self.assertEqual((it.title, it.author, it.year, it.lang), ("Les malices de Gaspard (10e série)", "Haurigot, Georges", "1867", "fr"))
        self.assertEqual(it.files[0]["url"], "https://gallica.bnf.fr/ark:/12148/bpt6k9633077k.pdf")
        self.assertEqual(sources.parse_gallica(self.GALLICA, ES), [])        # different language
        self.assertEqual(len(sources.parse_gallica(self.GALLICA, LANGS["French"])), 1)

    def test_xml_entities_rejected(self):
        with self.assertRaises(ValueError):
            sources.parse_gallica('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')

    def test_loc(self):
        data = {"results": [
            {"url": "http://www.loc.gov/item/2002719523/", "title": "El Quijote", "contributor": ["cervantes, miguel de"],
             "date": "1860", "language": ["spanish"]},
            {"url": "https://www.loc.gov/collections/x/", "title": "No es ítem"}]}
        r = sources.parse_loc(data)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].page, r[0].author, r[0].year), ("https://www.loc.gov/item/2002719523/", "cervantes, miguel de", "1860"))
        self.assertTrue(r[0].resolvers)
        item = {"resources": [{"pdf": "http://tile.loc.gov/x/book.pdf", "files": [
            [{"mimetype": "image/jp2", "url": "https://t/1.jp2"}, {"mimetype": "application/pdf", "url": "https://t/p1.pdf"},
             {"mimetype": "text/plain", "url": "https://t/p1.txt", "fulltext_service": "https://t/ocr1"}]]}]}
        files = sources.parse_loc_item(item)
        self.assertEqual([(f["ext"], f["url"]) for f in files],
                         [("pdf", "https://tile.loc.gov/x/book.pdf"), ("pdf", "https://t/p1.pdf"), ("txt", "https://t/ocr1")])
        self.assertEqual(sources.parse_loc_item({}), [])

    OPDS = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
      <entry><title> Libro  Libre </title><author><name>Ana Autora</name></author><dcterms:language>es</dcterms:language>
        <link rel="alternate" type="text/html" href="/work/1/"/>
        <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/download/1.epub"/>
        <link rel="http://opds-spec.org/acquisition/open-access" type="application/pdf" href="https://x.org/1.pdf"/>
        <link rel="http://opds-spec.org/image" type="image/png" href="/c.png"/></entry>
      <entry><title>En inglés</title><dcterms:language>en</dcterms:language><link rel="alternate" type="text/html" href="/w/2"/></entry>
      <entry><title></title></entry></feed>"""

    def test_opds(self):
        r = sources.parse_opds(self.OPDS, "https://unglue.it/api/opds/s.libro/", "Unglue.it")
        self.assertEqual(len(r), 2)
        self.assertEqual((r[0].title, r[0].author, r[0].page), ("Libro Libre", "Ana Autora", "https://unglue.it/work/1/"))
        self.assertEqual({(f["ext"], f["url"]) for f in r[0].files},
                         {("epub", "https://unglue.it/download/1.epub"), ("pdf", "https://x.org/1.pdf")})
        self.assertEqual([i.title for i in sources.parse_opds(self.OPDS, "https://u/", "U", ES)], ["Libro Libre"])

    def test_start_search_passes_page_wide_and_collects(self):
        calls, q = [], queue.Queue()

        def ok(text, L, loans, page=0, wide=False):
            calls.append((text, L, page, wide))
            return [make_item("X Libro", "A", None, "S", "https://p")]

        def bad(text, L, loans, page=0, wide=False):
            raise OSError("caído")
        sources.start_search("x", ES, True, 7, q, [("ok", ok), ("bad", bad)], page=2, wide=True)
        msgs = sorted((q.get(timeout=3) for _ in range(2)), key=lambda m: m[2])
        self.assertEqual([(m[0], m[1], m[2]) for m in msgs], [("err", 7, "bad"), ("res", 7, "ok")])
        self.assertEqual(calls, [("x", ES, 2, True)])

    def test_all_sources_accept_new_signature(self):
        import inspect
        for name, fn in sources.SOURCES:
            params = inspect.signature(fn).parameters
            self.assertEqual(list(params)[:5], ["q", "L", "loans", "page", "wide"], name)
        self.assertEqual(len(sources.SOURCES), len({n for n, _ in sources.SOURCES}))
        for name in ("Wikimedia Commons", "Library of Congress", "Gallica (BnF)", "Unglue.it", "Wolne Lektury"):
            self.assertIn(name, [n for n, _ in sources.SOURCES])

    def test_wolne_lektury_skipped_for_non_polish(self):
        self.assertEqual(sources.q_wolnelektury("pan tadeusz", ES), [])
        self.assertEqual(sources.q_unglue("x", ES, page=1), [])


class SpanishSourcesTests(unittest.TestCase):
    def test_stopwords_spanish(self):
        self.assertEqual(merge.query_words("una historia de amor"), ["historia", "amor"])
        self.assertEqual(merge.query_words("El Principito"), ["principito"])
        self.assertTrue(models.lang_ok("Castellano", ES))

    def test_zenodo_keeps_records_without_language(self):
        z = {"hits": {"hits": [{"id": 1, "metadata": {"title": "Sin idioma", "creators": []}, "files": []},
                               {"id": 2, "metadata": {"title": "Inglés", "language": "eng", "creators": []}, "files": []}]}}
        self.assertEqual([i.title for i in sources.parse_zenodo(z, ES)], ["Sin idioma"])

    def test_europeana(self):
        data = {"items": [
            {"title": ["Don Quijote de la Mancha"], "dcCreator": ["Cervantes Saavedra, Miguel de"], "year": ["1905"],
             "language": ["es"], "type": "TEXT", "guid": "https://www.europeana.eu/item/2020/abc?utm=1",
             "edmIsShownBy": ["https://bibliotecadigital.example.es/files/quijote.pdf"]},
            {"title": ["Quijote visor"], "language": ["es"], "type": "TEXT", "guid": "https://www.europeana.eu/item/1/x",
             "edmIsShownBy": ["https://viewer.example.es/view?id=3"]},
            {"title": ["Don Quixote"], "language": ["en"], "type": "TEXT", "guid": "https://www.europeana.eu/item/1/y"},
            {"title": ["Un cuadro"], "language": ["es"], "type": "IMAGE", "guid": "https://www.europeana.eu/item/1/z"},
            {"title": [], "guid": "x"}]}
        r = sources.parse_europeana(data, ES)
        self.assertEqual([i.title for i in r], ["Don Quijote de la Mancha", "Quijote visor"])
        self.assertEqual(r[0].files[0]["ext"], "pdf")
        self.assertEqual((r[0].author, r[0].year, r[0].lang), ("Cervantes Saavedra, Miguel de", "1905", "es"))
        self.assertEqual(r[1].files, [])                                   # web viewer: page only
        self.assertEqual(len(sources.parse_europeana(data, None)), 3)

    def test_wikibooks(self):
        r = sources.parse_wikibooks({"query": {"search": [{"title": "Álgebra lineal"}, {"title": "Álgebra/Cap 1"}]}}, ES)
        self.assertEqual([(i.title, i.lang) for i in r], [("Álgebra lineal", "es")])
        self.assertEqual(r[0].page, "https://es.wikibooks.org/wiki/%C3%81lgebra_lineal")

    def test_opds2_and_next_and_any(self):
        data = {"links": [{"rel": "next", "href": "/p2"}], "publications": [
            {"metadata": {"title": "Platero y yo", "author": [{"name": "Juan Ramón Jiménez"}], "language": "es",
                          "published": "1914-01-01"},
             "links": [{"rel": "http://opds-spec.org/acquisition/open-access", "type": "application/epub+zip", "href": "/d/1.epub"},
                       {"rel": "alternate", "type": "text/html", "href": "https://f.org/books/1"}]},
            {"metadata": {"title": "English one", "language": "en"}, "links": [{"rel": "alternate", "type": "text/html", "href": "/x"}]}],
            "groups": [{"publications": [{"metadata": {"title": "En grupo", "author": "Alguien"},
                                          "links": [{"rel": "http://opds-spec.org/acquisition", "type": "application/pdf", "href": "https://f.org/g.pdf"}]}]}]}
        body = json.dumps(data)
        r = sources.parse_opds_any(body, "https://f.org/search.json?query=x", "Feedbooks", ES)
        self.assertEqual([i.title for i in r], ["Platero y yo", "En grupo"])
        self.assertEqual((r[0].files[0]["url"], r[0].page, r[0].year, r[0].author),
                         ("https://f.org/d/1.epub", "https://f.org/books/1", "1914", "Juan Ramón Jiménez"))
        self.assertEqual(sources.opds_next(body, "https://f.org/search.json"), "https://f.org/p2")
        self.assertEqual(sources.opds_next("{no json", "https://f.org/"), "")
        self.assertEqual(sources.opds_next(TestOpds.ATOM_NEXT, "https://t.info/buscar?q=a"), "https://t.info/buscar?q=a&page=2")

    def test_expand_search(self):
        e = sources.expand_search
        self.assertEqual(e("https://x.org/s?query={searchTerms}&page={startPage?}", "don quijote"),
                         "https://x.org/s?query=don%20quijote")
        self.assertEqual(e("https://f.org/search.json{?query}", "ñu"), "https://f.org/search.json?query=%C3%B1u")
        self.assertEqual(e("https://x.org/s{?q,page}", "a"), "https://x.org/s?q=a")
        self.assertEqual(e("https://x.org/opds/s.{searchTerms}/", "a b"), "https://x.org/opds/s.a%20b/")

    def test_opds_discovery_and_source(self):
        root_atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>textos</title>
          <link rel="search" type="application/opensearchdescription+xml" href="/opensearch.xml"/></feed>"""
        osd = """<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
          <Url type="text/html" template="https://t.info/web?q={searchTerms}"/>
          <Url type="application/atom+xml" template="/buscar?q={searchTerms}&amp;page={startPage?}"/></OpenSearchDescription>"""
        results = TestOpds.RESULTS
        calls = []

        def fake(url, headers=None, use_cache=True):
            calls.append(url)
            if url.endswith("catalogo.atom"):
                return root_atom
            if url.endswith("opensearch.xml"):
                return osd
            if "/buscar?q=" in url:
                return results
            raise AssertionError(url)
        real = net.get_text
        net.get_text = fake
        try:
            sources._OPDS_SEARCH.clear()
            r = sources.q_textos("platero", ES)
            self.assertEqual([i.title for i in r], ["Platero y yo"])
            self.assertEqual(r[0].files[0]["ext"], "epub")
            self.assertTrue(any(u == "https://www.textos.info/buscar?q=platero" for u in calls), calls)
            n = len(calls)
            sources.q_textos("platero", ES)
            self.assertFalse(any(u.endswith("opensearch.xml") for u in calls[n:]))   # template remembered
            self.assertEqual(sources.q_textos("x", LANGS["English"]), [])             # Spanish only
        finally:
            net.get_text = real
            sources._OPDS_SEARCH.clear()

    def test_dspace7(self):
        def obj(uuid, title, typ, lang="spa"):
            return {"_embedded": {"indexableObject": {"uuid": uuid, "handle": "10261/" + uuid, "name": title, "metadata": {
                "dc.title": [{"value": title}], "dc.type": [{"value": typ}], "dc.language.iso": [{"value": lang}],
                "dc.contributor.author": [{"value": "Autora, Ana"}], "dc.date.issued": [{"value": "2015-05"}],
                "dc.identifier.uri": [{"value": "https://hdl.handle.net/10261/" + uuid}]}}}}
        data = {"_embedded": {"searchResult": {"_embedded": {"objects": [
            obj("u1", "Historia de la ciencia", "libro"), obj("u2", "Un artículo", "artículo"),
            obj("u3", "Book in English", "book", "eng")]}}}}
        r = sources.parse_dspace7(data, "Digital.CSIC", "https://digital.csic.es", "https://digital.csic.es/server/api", ES)
        self.assertEqual([i.title for i in r], ["Historia de la ciencia"])
        self.assertEqual((r[0].author, r[0].year, r[0].lang, r[0].page), ("Autora, Ana", "2015", "es", "https://hdl.handle.net/10261/u1"))
        self.assertTrue(r[0].resolvers)
        item = {"_embedded": {"bundles": {"_embedded": {"bundles": [
            {"name": "THUMBNAIL", "_embedded": {"bitstreams": {"_embedded": {"bitstreams": [
                {"name": "x.jpg", "_links": {"content": {"href": "https://h/t"}}}]}}}},
            {"name": "ORIGINAL", "_embedded": {"bitstreams": {"_embedded": {"bitstreams": [
                {"name": "libro.pdf", "_links": {"content": {"href": "https://h/api/bitstreams/1/content"}}},
                {"name": "libro2.pdf", "_links": {"content": {"href": "https://h/api/bitstreams/2/content"}}},
                {"name": "datos.csv", "_links": {"content": {"href": "https://h/api/bitstreams/3/content"}}}]}}}}]}}}}
        files = sources.parse_dspace7_item(item, "Digital.CSIC")
        self.assertEqual([(f["ext"], f["url"]) for f in files], [("pdf", "https://h/api/bitstreams/1/content")])
        self.assertEqual(sources.parse_dspace7({}, "x", "u", "a", ES), [])

    def test_core(self):
        data = {"results": [
            {"id": 113862, "title": "El libro de las escuelas", "authors": [{"name": "Cervantes"}], "yearPublished": 1905,
             "downloadUrl": "https://core.ac.uk/download/pdf/113862.pdf", "language": {"code": "es", "name": "Spanish"}},
            {"id": 2, "title": "English paper", "language": {"code": "en"}}]}
        r = sources.parse_core(data, ES)
        self.assertEqual([(i.title, i.files[0]["ext"], i.lang) for i in r], [("El libro de las escuelas", "pdf", "es")])
        self.assertEqual(r[0].page, "https://core.ac.uk/works/113862")

    def test_core_needs_key_and_new_sources_registered(self):
        net.STATE["core_key"] = ""
        self.assertEqual(sources.q_core("x", ES), [])                       # no key: silent
        names = [n for n, _ in sources.SOURCES]
        for n in ("Europeana", "textos.info", "Feedbooks", "Wikibooks", "DSpace repositories", "CORE"):
            self.assertIn(n, names)
        self.assertGreaterEqual(len(sources.SOURCES), 21)
        self.assertGreater(len(sources.WEB_SITES), 20)
        for name, fn, needs in sources.WEB_SITES:
            url = fn("don quijote")
            self.assertTrue(models.safe_url(url), name)

    def test_settings_new_keys(self):
        tmp = tempfile.mkdtemp()
        settings._DATA_DIR = tmp
        try:
            with open(settings.settings_path(), "w", encoding="utf-8") as f:
                json.dump({"core_key": "abc", "europeana_key": 7}, f)
            cfg = settings.load_settings()
            self.assertEqual((cfg["core_key"], cfg["europeana_key"]), ("abc", ""))
        finally:
            settings._DATA_DIR = None


class TestOpds:
    ATOM_NEXT = """<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="?q=a&amp;page=2" type="application/atom+xml"/></feed>"""
    RESULTS = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
      <entry><title>Platero y yo</title><author><name>Juan Ramón Jiménez</name></author><dcterms:language>es</dcterms:language>
        <link rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip" href="/descargar/1.epub"/>
        <link rel="alternate" type="text/html" href="/libro/platero"/></entry></feed>"""


class MergePerformanceTests(unittest.TestCase):
    WORDS = ("historia amor guerra paz vida muerte tiempo mundo hombre mujer casa noche dia sol luna mar tierra cielo "
             "fuego agua sombra rey reina dios libro cuento poemas ciencia arte derecho economia medicina teoria "
             "practica manual curso estudio ensayo diario cartas viaje jardin ciudad pueblo camino").split()
    AUTH = "garcia lopez martinez sanchez perez gomez ruiz diaz moreno alvarez romero navarro torres dominguez".split()

    def data(self, n, seed=1):
        import random
        rnd = random.Random(seed)
        return [make_item(" ".join(rnd.sample(self.WORDS, rnd.randint(2, 6))).title(),
                          rnd.choice(self.AUTH) + " " + rnd.choice(self.AUTH), None, "S", "https://p/%d" % i)
                for i in range(n)]

    def test_indexed_store_matches_brute_force(self):
        """The word index must merge exactly the same as comparing against everything."""
        fast, slow = merge.Store(), []
        for it in self.data(700):
            fast.merge(it)
        for it in self.data(700):
            for old in slow:
                if merge._same_book(old, it):
                    break
            else:
                slow.append(it)
        self.assertEqual(len(fast.items), len(slow))
        self.assertEqual(sorted(i.title for i in fast.items), sorted(i.title for i in slow))

    def test_merge_scales(self):
        """Regression: 3000 books used to take ~50 s and freeze the window (quadratic)."""
        import time
        st, t0 = merge.Store(), time.time()
        for it in self.data(3000):
            st.merge(it)
        self.assertLess(time.time() - t0, 10)

    def test_author_filled_later_refreshes_cache(self):
        st = merge.Store()
        a = make_item("La Celestina", "", None, "A", "p")
        st.merge(a)
        st.merge(make_item("La Celestina", "Fernando de Rojas", None, "B", "p"))
        # now "a" has an author: another book with the same title and a different author must NOT merge
        _, new = st.merge(make_item("La Celestina", "Pablo Neruda", None, "C", "p"))
        self.assertTrue(new)
        _, new = st.merge(make_item("La Celestina", "Rojas, Fernando", None, "D", "p"))
        self.assertFalse(new)


# ---------- Network against a local server ----------
HITS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/octet-stream", length=None, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        if p == "/ok.json":
            self._send(b'{"a": 1}', "application/json")
        elif p == "/flaky.json":
            if HITS[p] == 1:
                self._send(b"", code=429, extra={"Retry-After": "0"})
            else:
                self._send(b'{"ok": true}', "application/json")
        elif p == "/always429.json":
            self._send(b"", code=429)
        elif p == "/missing.json":
            self._send(b"", code=404)
        elif p == "/book.pdf":
            self._send(b"%PDF-1.4\n" + b"x" * 1000, "application/pdf")
        elif p == "/page.pdf":
            self._send(b"<!DOCTYPE html><html>login</html>", "text/html")
        elif p == "/short.pdf":
            self._send(b"%PDF-1.4 corto", length=5000)
        elif p == "/big.pdf":
            self._send(b"%PDF-1.4\n" + b"x" * 3_000_000, "application/pdf")
        else:
            self._send(b"", code=404)


class NetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        net.BACKOFF = (0, 0)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        HITS.clear()
        net._cache.clear()
        net.bind_cancel(None)
        self.tmp = tempfile.mkdtemp()

    def test_json_cache(self):
        self.assertEqual(net.get_json(self.base + "/ok.json"), {"a": 1})
        net.get_json(self.base + "/ok.json")
        self.assertEqual(HITS["/ok.json"], 1)
        net.get_json(self.base + "/ok.json", use_cache=False)
        self.assertEqual(HITS["/ok.json"], 2)

    def test_retry_on_429_then_ok(self):
        self.assertEqual(net.get_json(self.base + "/flaky.json"), {"ok": True})
        self.assertEqual(HITS["/flaky.json"], 2)

    def test_gives_up_after_retries_and_404_not_retried(self):
        with self.assertRaises(Exception):
            net.get_json(self.base + "/always429.json")
        self.assertEqual(HITS["/always429.json"], 3)  # 1 + 2 retries
        with self.assertRaises(Exception):
            net.get_json(self.base + "/missing.json")
        self.assertEqual(HITS["/missing.json"], 1)

    def test_jget_treats_404_as_empty_but_not_other_errors(self):
        self.assertEqual(sources._jget(self.base + "/missing.json"), {})
        with self.assertRaises(Exception):
            sources._jget(self.base + "/always429.json")

    def test_get_text_returns_str(self):
        self.assertIn('"a"', net.get_text(self.base + "/ok.json"))

    def test_refuses_file_scheme(self):
        p = os.path.join(self.tmp, "secreto.pdf")
        with open(p, "wb") as f:
            f.write(b"%PDF-secreto")
        out = os.path.join(self.tmp, "copia.pdf")
        with self.assertRaises(ValueError):
            net.download("file://" + p, out, "pdf")
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(ValueError):
            net.get_json("file://" + p)

    def test_download_ok(self):
        out, seen = os.path.join(self.tmp, "b.pdf"), []
        net.download(self.base + "/book.pdf", out, "pdf", lambda d, t: seen.append((d, t)))
        with open(out, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"%PDF"))
        self.assertTrue(seen and seen[-1][0] == seen[-1][1])
        self.assertFalse(os.path.exists(out + ".part"))

    def test_download_rejects_html(self):
        out = os.path.join(self.tmp, "x.pdf")
        with self.assertRaises(ValueError):
            net.download(self.base + "/page.pdf", out, "pdf")
        self.assertFalse(os.path.exists(out) or os.path.exists(out + ".part"))

    def test_download_truncated_detected(self):
        out = os.path.join(self.tmp, "t.pdf")
        with self.assertRaises(Exception):
            net.download(self.base + "/short.pdf", out, "pdf")
        self.assertFalse(os.path.exists(out) or os.path.exists(out + ".part"))

    def test_download_cancel(self):
        out, ev = os.path.join(self.tmp, "c.pdf"), threading.Event()
        with self.assertRaises(net.Cancelled):
            net.download(self.base + "/big.pdf", out, "pdf", lambda d, t: ev.set(), ev)
        self.assertFalse(os.path.exists(out) or os.path.exists(out + ".part"))

    def test_cancelled_search_thread_stops(self):
        ev = threading.Event()
        ev.set()
        net.bind_cancel(ev)
        with self.assertRaises(net.Cancelled):
            net.get_json(self.base + "/ok.json")

    def test_variants_parallel_and_partial_failure(self):
        def fn(v):
            if v == "bad":
                raise RuntimeError("x")
            return [v]
        self.assertEqual(sources._variants(fn, ["a", "bad", "b"]), ["a", "b"])
        with self.assertRaises(RuntimeError):
            sources._variants(fn, ["bad"])

    def test_isbn_skips_text_only_sources(self):
        self.assertEqual(sources.q_gutendex("9780306406157"), [])
        self.assertEqual(sources.q_zenodo("0306406152"), [])


class SettingsTests(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        tmp = tempfile.mkdtemp()
        settings._DATA_DIR = tmp
        try:
            with open(settings.settings_path(), "w", encoding="utf-8") as f:
                json.dump({"sites": [{"name": "ok", "url": "https://x.org/?q={q}"},
                                     {"name": "bad", "url": "file:///c:/"}, "junk"],
                           "lang": "English", "loans": 1, "history": ["a", 3, "b"], "google_key": 5}, f)
            cfg = settings.load_settings()
            self.assertEqual([s["name"] for s in cfg["sites"]], ["ok"])
            self.assertEqual((cfg["lang"], cfg["loans"], cfg["history"], cfg["google_key"]),
                             ("English", True, ["a", "b"], ""))
            cfg["contact"] = "me@example.org"
            self.assertTrue(settings.save_settings(cfg))
            self.assertEqual(settings.load_settings()["contact"], "me@example.org")
            with open(settings.settings_path(), "w") as f:
                f.write("{not json")
            self.assertEqual(settings.load_settings()["lang"], "English")
        finally:
            settings._DATA_DIR = None

    def test_build_url(self):
        self.assertEqual(settings.build_url("https://a.org/?q={q}", "don quijote"),
                         "https://a.org/?q=don+quijote")


if __name__ == "__main__":
    unittest.main()
