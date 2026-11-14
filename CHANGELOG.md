# Changelog

## 1.0.0

First release as **Bookfinch** (previously *BuscaLibros*, a Spanish-language project).

- Renamed the project and the Python package (`buscalibros` → `bookfinch`).
- The whole interface, messages, CSV headers, source labels, code comments, documentation and tests are now in English.
- New logo: a finch whose wing is made of book pages, perched on a book (`assets/logo.svg`).
  All icons (`.ico`, window icon, PNG) are generated from that single SVG by `tools/make_icon.py`.
- Language names in the drop-down are now English (*Spanish*, *French*, …) and the default language is *English*.
  Filter names are *Flexible*, *Strict* and *No filter*; sort options and column headers are English too.
- Audiobooks are recognised by the title marker `(audiobook)` (the Spanish `(audiolibro)` is still recognised).
- Fixed the status message of ISBN searches ("Searching ISBN in N sources…"), which used to run words together.
- Amazon and Europeana web shortcuts now point to their English-language sites.
- The settings file is now `settings.json` and the portable data folder is `data` (it was `ajustes.json` / `datos`).
  Settings are **not** migrated from BuscaLibros; the app starts with defaults.
- CI: tests now run on Python 3.9–3.13 (Ubuntu) before the Windows build.
- Added `LICENSE` (MIT), `CHANGELOG.md`, `.gitattributes` and an expanded `.gitignore`.

---

# History before the rename

The versions below belong to the former *BuscaLibros* project, renumbered to lead up to 1.0.0
(old 3.x became 0.x).

## 0.2.2: fix for the freeze on close

**Cause:** with 21 sources, thousands of results arrive and duplicate merging compared each book with every previous
one (quadratic cost: 3,000 books ≈ 51 s with the window frozen, not even responding to the close button).
**Fix:** a word index (3,000 books ≈ 0.45 s, identical result), messages processed in slices of ≤ 30 ms, table
re-sorting limited to once every 0.5 s and only for the rows that change, and an immediate process exit on close.
Covered by equivalence and performance tests.

## 0.2: better Spanish coverage

**Why few Spanish books showed up:** almost everything that is legally free is public domain or released by its
authors, and the big catalogues are mostly English (Project Gutenberg has about 1 % in Spanish). Modern Spanish books
are nearly all under copyright, so no legal tool can show them. What can be done is to reach *all* the open Hispanic
collections and not lose books on technicalities.

**6 new sources:** Europeana (aggregates Hispana, the BNE, the Biblioteca Virtual Miguel de Cervantes and hundreds of
libraries; uses a public demo key, better with your own), textos.info (Spanish catalogue), Feedbooks (multilingual
public domain), Wikibooks, Digital.CSIC and e-Archivo UC3M repositories (only records that look like books) and CORE
(university repositories; only enabled if you paste a free key in ⚙ Settings). The search endpoints of textos.info and
Feedbooks are **discovered automatically** through the OPDS standard, with no guessed addresses.

**Fixes that help Spanish:** Spanish stop words ("un", "una", "por", "con", "para"…) no longer count when filtering,
so "una historia de amor" does not require "una" in the title · records with no language annotation (very common in
Zenodo) are no longer discarded · "Castellano" is recognised as Spanish.

**15 more Spanish-language sites in the "Also search the web" menu** (no public API): Elejandría, Biblioteca Virtual
Universal, Hispana, Libroteca, Freeditorial, El Libro Total, Alibrate, Lektu, InfoLibros, Libros CSIC, CLACSO,
Biblioteca Nacional Digital de Chile, Dialnet and Redalyc.

**What cannot be done (and was not):** the Biblioteca Virtual Miguel de Cervantes and the Biblioteca Digital Hispánica
offer no search API with downloads (the BVMC only has a SPARQL metadata endpoint); part of their content arrives
through Europeana and the web menu. No piracy download sites are included.

**Honest warning:** these sources were written from their documentation but could not be tested against the real
servers. The DSpace 7 repositories (do those two really use `/server/api`?) and the Europeana demo key are the most
uncertain points. If one fails, the status bar says so and the rest keeps working.

## 0.1: many more books

**5 new sources** (from the `public-apis` list and other keyless open APIs): Wolne Lektury (Polish literature, the
only download-capable entry in public-apis' *Books* section that was missing), Library of Congress (~90,000 digitised
books), Wikimedia Commons (scanned books in PDF/DjVu), Gallica (Bibliothèque nationale de France) and Unglue.it (OPDS
catalogue of free ebooks). From public-apis, the Bible/Quran/poetry APIs (not downloadable books) and the
metadata-only ones (Crossref, British National Bibliography, Penguin, BookRank) were left out: OpenAlex already
brings Crossref's data with open links.

**Ways to get more results:**
- **Automatic widening:** if a search returns fewer than 40 books, it repeats itself in *all languages*, without stop
  words and with single keywords. The chosen language becomes a *preference* (+5 relevance) instead of a filter that
  discards anything without a language annotation.
- **＋ More results:** requests the next page from each source (Gutendex, Google, Archive, Open Library, DOAB…).
- **More per query:** up to 100 results per request (was 25–60), several pages of Gutendex and Google Books, topic
  search on Gutenberg, an accent-free variant and Wikisource in 6 languages under *All languages*.
- **Free lending enabled by default** (shown in grey, with a link to the page).
- **Language column** unified to 2 letters (es, en, fr…), sortable, with books in your language ranked first.

**Honest warning:** the 5 new sources were written from their documentation and could not be tested against the live
servers. If one changes or fails, the app says so in the status bar and the rest keeps working; details go to the log.
Two specific points to watch: the Wolne Lektury search (`?search=`) and Gallica's `.pdf` link; if they do not work,
the download is rejected with a clear message instead of saving a corrupt file.

## 0.0: initial rewrite (compared with the previous version)

**Bugs fixed:** Gutenberg `.txt` in UTF-8 (it could pick ASCII and lose accents) · only `http/https` URLs are accepted
(`urllib` used to open `file://`) · correct state when re-selecting a book while its formats load, with a *Retry*
button on failure · duplicate merging across different subtitles ("Don Quijote…" ≈ "El ingenioso hidalgo Don
Quijote…") without mixing audiobooks or different authors · `es_ES`/`es-ES` codes accepted · incomplete downloads
detected · network threads decoupled from the interface through a queue · Enter no longer starts overlapping
searches · "Wikisource" no longer appears as an author.

**Interface:** visible errors (which sources did not answer) · cancel search and download · global 45 s limit ·
search variants in parallel · crisp on HiDPI screens · remembers folder, language, filter and history · *Show file*
after downloading · CSV export · context menu · ISBN search.

**Network:** retries with back-off on 429/5xx · 5-minute cache (repeating a search or changing a filter is instant and
does not hammer the free APIs) · gzip · User-Agent with contact · optional Google Books key.

**Project:** code split into modules · 60 tests · log rotation · portable mode · `.exe` icon and metadata ·
reproducible build (isolated environment, PyInstaller `>=6.11,<7`, no UPX) · CI on GitHub Actions.
