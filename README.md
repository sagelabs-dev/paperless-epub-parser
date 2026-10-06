# paperless-epub-parser

An **EPUB parser plugin for [Paperless-ngx](https://docs.paperless-ngx.com/)** —
makes `.epub` files consumable and full-text searchable.

No fork of Paperless-ngx. The upstream project exposes a supported parser
plugin mechanism: any Python package that advertises a class under the
`paperless_ngx.parsers` entry-point group is discovered automatically at
startup and competes with the built-in parsers on score. This package is that,
and nothing more.

## Why

Paperless-ngx has no built-in EPUB support. Neither Apache Tika (whose Paperless
mime list is hardcoded to Office formats) nor Gotenberg/LibreOffice (which has an
EPUB *export* filter but no *import* filter) can open one. This plugin supplies
the missing parser, so an EPUB library becomes searchable alongside everything
else in the archive.

Scope is deliberately narrow: **text for search**. No archive PDF, no cover-art
thumbnail, no page count. See "Design decisions" below.

## Install

The container's `site-packages` is root-owned and the image is ephemeral, so a
hand-run `pip install` inside a running container does not persist. Build a thin
derived image instead:

```dockerfile
FROM ghcr.io/paperless-ngx/paperless-ngx:3.1.3

RUN pip install --no-cache-dir paperless-epub-parser
```

Then point the `webserver` service at it:

```yaml
services:
  webserver:
    build: .
    # ... rest of your existing service definition
```

⚠️ **Do not add a `USER` instruction to this Dockerfile.** It is the obvious
thing to write and it will break your instance. See "The `USER` trap" below.

⚠️ **Pin the base image tag.** The upstream `latest` tag moves; a rebuild months
later would silently install the plugin into a different Paperless version than
the one you tested against.

Verify discovery in the startup log:

```
Loaded third-party parser 'EPUB' v1.1.0 by David Newman (entrypoint: 'epub').
```

## The `USER` trap

If you write the natural-looking Dockerfile — `USER root`, install, `USER
paperless` — **the container will not start.** It exits cleanly, forever:

```
Restarts=3, climbing · ExitCode=0 · no error output
```

That signature is the confusing kind. A clean exit with no message reads like
"nothing to do", not "broken", so it is easy to spend a while looking in the
wrong place.

The cause: the official image declares **no `USER` at all**.

```bash
docker inspect ghcr.io/paperless-ngx/paperless-ngx:3.1.3 --format '{{.Config.User}}'
# -> (empty)
```

It runs `/init` (s6-overlay) **as root** and drops privileges *internally*, to
the uid/gid given by `USERMAP_UID` / `USERMAP_GID`. Setting `USER paperless`
yourself pre-empts that handoff, and init exits.

**So: install your package and stop.** Add no `USER` line; the base image owns
privilege dropping.

If you already have a crashing instance: `Restarts` climbing, `ExitCode=0`, no
errors — remove the `USER` instruction and rebuild.

## Design decisions

**Text extraction is delegated to [`markitdown`](https://github.com/microsoft/markitdown).**
Its `EpubConverter` already implements the genuinely fiddly parts: locating the
OPF package document via `META-INF/container.xml`, resolving manifest hrefs
(including percent-encoded ones), walking the spine in *reading order* rather
than filename order, and converting each XHTML chapter to Markdown with headings
and tables preserved. Reimplementing that would duplicate a maintained upstream.

EPUB conversion lives in markitdown's **base** install — the converter subclasses
`HtmlConverter` and needs only `beautifulsoup4`, which is a core dependency.
There is no `[epub]` extra and none is required, so this plugin depends on bare
`markitdown` and deliberately does **not** pull in the PDF/DOCX/PPTX/XLSX extras
(Paperless already handles those formats natively).

**No archive PDF.** EPUB is the readable artifact; it belongs in an e-reader, not
in a PDF viewer. `can_produce_archive` and `requires_pdf_rendition` are both
`False`, so the consumer skips PDF generation entirely.

**The thumbnail is rendered text, not cover art.** This follows the precedent set
by Paperless-ngx's own `TextDocumentParser`. Cover extraction would mean adding
`ebooklib` plus, for the common case of an SVG or fixed-layout cover, an SVG
rasteriser — two dependencies and a whole class of failure (missing, oversized,
malformed, DRM-wrapped covers) in exchange for a nicer grid tile.

**`get_page_count()` returns `None`.** EPUB is a reflowable format. It has no
pages. Any integer would be an invention.

**`get_date()` returns `None`.** EPUB `dc:date` is unreliable in practice — often
a build timestamp rather than a publication date, and absent altogether from the
fixtures this plugin was validated against. Paperless's own date parser reads the
filename and extracted text and does better than a blind metadata grab.

**Fully local.** The parser declares `uses_remote_service = False` and never
contacts anything off-box.

## Known upstream defect, and the correction

`markitdown`'s `EpubConverter` locates the spine with
`getElementsByTagName("itemref")` and the manifest with
`getElementsByTagName("item")`. Both match on the **qualified** name, so on an
OPF written with namespace prefixes — `<opf:itemref>`, `<opf:item>` — those
lookups return **zero** elements. The converter then finds no spine, emits only
the metadata block, and returns a few hundred characters for a book of several
hundred thousand. **Nothing raises.**

That syntax is legal EPUB 2 and is emitted by several publisher toolchains. In a
795-book library it affected exactly one book — which is precisely why it was
worth fixing: a hole that size is invisible when you read the output.

This plugin detects that specific condition before delegating, and takes a local
spine-walking path instead (`_epub.py`). Detection is the precise condition, not
a heuristic on output length, so books `markitdown` handles correctly are never
rerouted. Only the spine walk is replaced — each document is still converted by
`markitdown`'s own `HtmlConverter`, so HTML-to-Markdown behaviour is unchanged.

Measured on the affected book: **203 → 157,367 characters**, with all other
books byte-identical.

## Non-conformant EPUBs detected as `application/zip`

Some EPUBs are rejected by Paperless before this — or any — parser is consulted:

```
ConsumerError: book.epub: Unsupported mime type application/zip
```

The book is fine. Its *container* is not. The OCF specification requires a
`mimetype` entry as the **first record** in the archive, stored uncompressed, so
that a reader can identify the format from the opening bytes. Plenty of real
files violate this — some list a directory entry first, some put `mimetype`
third.

Typesetting it correctly matters because Paperless identifies files with
`libmagic`, which reads the leading bytes and reports `application/zip` for a
misordered archive. The consumer then asks the parser registry for a handler for
`application/zip`, and — note the ordering — **a parser whose declared MIME types
omit the detected type is skipped outright, so its `score()` is never called.**

That ordering is why this plugin declares `application/zip` at all. It is not a
claim to handle ZIP files; it is the only way to be *offered* the file. The claim
is then gated inside `score()`, which opens the archive and requires **both**
structural markers together:

- a `META-INF/container.xml` entry, and
- a `mimetype` entry whose content is exactly `application/epub+zip`.

The `mimetype` entry's *position* is deliberately ignored, since position is
precisely the thing that varies.

Because `application/zip` is a busy type, the claim is scored **1**, not 10 — an
archive must never out-rank a parser that genuinely handles whatever the file is.
An ODT, for instance, also carries a `mimetype` entry; only its value differs,
and it is declined. docx, xlsx, jar, apk and plain zip are all declined.

If your archive does not need ZIP files handled by anything else, this is
transparent. If you run a plugin that legitimately handles ZIP archives, it will
out-score this one and win.

## Failure behaviour

- **Malformed / unreadable file** → `ParseError` naming the file, so Paperless
  reports a document error rather than an "unexpected error".
- **DRM-protected or fixed-layout EPUB** → no extractable flow text, which is
  raised as a `ParseError` with a message that says so. It is not silently filed
  as an empty document.
- **Thumbnail generation never raises.** The consumer calls it unguarded, so a
  failure there would abort an otherwise successful consumption.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -v
```

The suite stubs the conversion seam (`_extract_epub`), so it runs without
`markitdown`, Paperless-ngx, or Django installed. It covers the registry
contract, the protocol surface, the failure paths, and the thumbnail guarantee.

Real conversion is verified separately against genuine EPUBs — see below.

## Verification

Unit tests prove the contract. They do not prove that `markitdown` extracts
useful text from a real book. To check that, and to confirm end-to-end discovery
by Paperless:

```bash
# 1. Extraction, standalone
uv venv /tmp/v && uv pip install --python /tmp/v/bin/python markitdown
/tmp/v/bin/python -c "
from markitdown import MarkItDown
r = MarkItDown().convert('book.epub')
print(len(r.text_content or r.markdown or ''))
"

# 2. Discovery, via the container's startup log
docker compose logs webserver | grep 'Loaded third-party parser'

# 3. End-to-end: drop an EPUB into consume/ and watch it become searchable
cp book.epub /path/to/paperless/consume/
```

## Compatibility

- Paperless-ngx **3.1.3** (the parser registry landed in the 3.x series; this
  plugin targets the `ParserProtocol` interface as it exists in 3.1.3).
- Python **3.11+** (the image ships 3.14; `markitdown` supports 3.10–3.14).

## Reporting problems

Please include the book that misbehaved, or at least:

- the exact error from `docker compose logs webserver`
- the output of `file yourbook.epub`
- the first few entries of `unzip -l yourbook.epub`

Those three lines distinguish "the container is non-conformant" from "the text
extraction failed", which are different problems with different fixes.

## Licence

MIT — see [LICENSE](LICENSE).

Copyright (c) 2026 David Newman and Guan.

## Sponsors

If paperless-epub-parser is useful to you, consider supporting its continued development:

- **[GitHub Sponsors](https://github.com/sponsors/guan-tends)**
- **Bitcoin:** `bc1q0gd3mwjg3zy9sghv22kmpg823vss4c0zzdzg24`
- **Solana:** `Eu8wQcW68TKMs1a6eqzZu8znzU52QLqQugAMG8uCD6y6`
- **Ethereum / EVM:** `0x2733ff7c865C56d565a99BE1DC11B81cc76850A5`
- **XRP Ledger:** `r4X6e7McAQj7e8vBCeued1RYu4mCJrREDG`

---

Crafted with ❤️ by [Sage Labs](https://sagelabs.dev)
