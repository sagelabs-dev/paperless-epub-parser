"""EPUB parser plugin for Paperless-ngx.

Paperless-ngx discovers third-party document parsers by scanning the
``paperless_ngx.parsers`` Python entry-point group at application startup
(see ``paperless/parsers/registry.py`` upstream).  This package advertises
:class:`EpubDocumentParser` under that group, which makes ``.epub`` files
consumable and full-text searchable without forking Paperless-ngx.

Design notes
------------
**Text extraction** is delegated wholesale to Microsoft's ``markitdown``
library, whose ``EpubConverter`` already implements the hard parts: parsing
``META-INF/container.xml`` to locate the OPF package document, resolving
manifest hrefs, walking the spine *in reading order* rather than filename
order, and converting each XHTML chapter to Markdown with headings and tables
preserved.  Reimplementing any of that would duplicate a maintained, tested
upstream.  We are a thin adapter, not a converter.

**Thumbnails** follow the precedent set by Paperless-ngx's own
``TextDocumentParser``: render a portion of the extracted text onto a plain
canvas.  We deliberately do *not* chase cover art.  Cover extraction would add
``ebooklib`` plus, in the common fixed-layout case, an SVG rasteriser -- two
dependencies and a class of failure (missing, oversized, malformed and
DRM-wrapped covers) in exchange for a nicer grid tile.  Text is what this
document exists to expose; the thumbnail should reflect that.

**Archive PDFs are not produced.**  This plugin is explicitly text-first: the
EPUB *is* the readable artifact and belongs in an e-reader, not in Paperless's
PDF viewer.  ``can_produce_archive`` and ``requires_pdf_rendition`` are both
``False`` so the consumer skips PDF generation entirely.

**Page count is ``None``.**  EPUB is a reflowable format; it has no pages.
Any integer here would be an invention.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Self

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from paperless.parsers import MetadataEntry
    from paperless.parsers import ParserContext

__all__ = ["EpubDocumentParser", "ParseError"]

# Single source of truth for the version. ``pyproject.toml`` declares
# ``dynamic = ["version"]`` and reads this value at build time, so the package
# metadata and the value Paperless logs at startup can never disagree.
__version__ = "1.1.0"


class ParseError(Exception):
    """Raised when an EPUB cannot be parsed.

    Mirrors ``documents.parsers.ParseError``.  We import that class lazily at
    the call site (see :meth:`EpubDocumentParser.parse`) so this module stays
    importable in a plain test environment without Django configured, while
    still raising the exact type the consumer's ``except ParseError`` clause
    catches.
    """


# MIME types this parser claims.  ``application/epub+zip`` is the registered
# type; the others appear in the wild from older tooling.  Python's stdlib
# ``mimetypes`` module already maps ``.epub`` to ``application/epub+zip``, so
# declaring it here is sufficient to make the extension consumable --
# Paperless derives its consumable-extension set from what parsers declare
# (``documents/parsers.py::get_supported_file_extensions``).
_SUPPORTED_MIME_TYPES: dict[str, str] = {
    "application/epub+zip": ".epub",
    "application/epub": ".epub",
    "application/x-epub+zip": ".epub",
    # ``application/zip`` is claimed deliberately -- see :meth:`score`.
    "application/zip": ".epub",
}

# MIME types that identify an EPUB outright, with no further inspection needed.
_EPUB_MIME_TYPES = frozenset(
    {"application/epub+zip", "application/epub", "application/x-epub+zip"},
)

# MIME types that *may* be an EPUB but need the archive inspected to confirm.
_ZIP_MIME_TYPES = frozenset({"application/zip"})

# Score 10 matches the built-in parsers.  Nothing else in Paperless-ngx claims
# EPUB, so there is no contention to win -- but keeping the conventional value
# means a future built-in EPUB parser can be overridden deliberately rather
# than accidentally.
_SCORE = 10

# Just above zero.  When a file's *content* looks like a plain ZIP we still
# accept it, but only after inspecting it: an unrecognised archive must not
# out-rank a genuine parser for whatever the file actually is.
_ZIP_ALIAS_SCORE = 1


def _looks_like_epub(path: Path) -> bool:
    """Decide whether a ZIP archive is really an EPUB.

    Two independent structural signals are required, matching what the OCF
    container specification defines.  Either alone produces false positives:
    ``mimetype`` appears in unrelated archives, and ``container.xml`` can be
    present in a malformed file.  Both together are a reliable EPUB marker.

    This deliberately does *not* trust the ``mimetype`` entry's *position* in
    the archive.  Its position is the very thing that varies -- a file with
    ``mimetype`` present but not first is a common, readable, non-conformant
    EPUB, and accepting it here is the entire purpose of this function.

    Parameters
    ----------
    path:
        Filesystem path to the candidate archive.

    Returns
    -------
    bool
        ``True`` when the archive carries both EPUB structural markers.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/container.xml" not in names or "mimetype" not in names:
                return False
            declared = archive.read("mimetype").strip()
    except (OSError, zipfile.BadZipFile, KeyError):
        # A file libmagic called a ZIP but that we cannot open is not our
        # problem to solve -- decline and let Paperless report it normally.
        return False
    return declared == b"application/epub+zip"

# Thumbnail geometry required by Paperless-ngx (~500x700 WebP).
_THUMB_SIZE = (500, 700)
_THUMB_FONT_SIZE = 18
_THUMB_PADDING = 8
_THUMB_SPACING = 4
# How much extracted text to paint onto the thumbnail.  The canvas holds
# roughly a page; more text simply falls off the bottom.
_THUMB_TEXT_CHARS = 1200


class EpubDocumentParser:
    """Extract searchable text from EPUB files for Paperless-ngx.

    Satisfies Paperless-ngx's structural ``ParserProtocol``.  No base class is
    required -- the registry validates attribute presence at discovery time
    and never instantiates the class to do so, which is why the identity
    attributes below are plain class attributes rather than properties.

    Instances are always used as context managers: ``__enter__`` opens a
    scratch directory for intermediate files and ``__exit__`` removes it,
    including on the exception path.
    """

    # -- Identity ---------------------------------------------------------
    # Read by the registry *before instantiation*, so these must be class
    # attributes.  They appear in the startup log line and let an operator
    # identify which third-party package handled a document.
    name: str = "EPUB"
    version: str = __version__
    author: str = "David Newman"
    url: str = "https://github.com/sagelabs-dev/paperless-epub-parser"

    # Marks this parser as fully local.  Paperless-ngx excludes parsers that
    # declare ``uses_remote_service`` unless the document was explicitly
    # flagged for remote processing; we never contact anything off-box.
    uses_remote_service: ClassVar[bool] = False

    def __init__(self, logging_group: object = None) -> None:
        """Create a parser with its own scratch directory.

        Parameters
        ----------
        logging_group:
            Optional Paperless-ngx logging group.  Unused, accepted for
            parity with the built-in parsers' constructor signature.
        """
        self._logging_group = logging_group
        self._text: str = ""
        self._title: str | None = None
        self._tempdir = Path(tempfile.mkdtemp(prefix="paperless-epub-"))

    # -- Context manager --------------------------------------------------
    # Paperless-ngx always drives parsers as context managers, so intermediate
    # files live inside a directory whose lifetime matches the parse.

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # ``ignore_errors`` because a failed cleanup must never mask the real
        # parse error propagating out of the ``with`` block.
        shutil.rmtree(self._tempdir, ignore_errors=True)

    # -- Registry contract ------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        """Return the MIME type to preferred-extension mapping."""
        return dict(_SUPPORTED_MIME_TYPES)

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        """Return this parser's priority for ``mime_type``.

        Returns ``None`` to decline handling a file.  Paperless-ngx calls this
        on every candidate parser and keeps the highest score, breaking ties
        in favour of third-party parsers over built-ins.
        """
        if mime_type in _EPUB_MIME_TYPES:
            return _SCORE

        # ``application/zip`` needs a second opinion.  Detection is performed
        # by libmagic, which classifies a ZIP by its first bytes alone and
        # therefore reports ``application/zip`` for any EPUB whose ``mimetype``
        # entry is not the first thing in the archive -- which the OCF
        # specification requires but many real-world files violate.  Such a
        # book is a perfectly readable EPUB that Paperless would otherwise
        # reject with "Unsupported mime type application/zip" before any
        # parser is consulted.
        #
        # Note the ordering constraint in ``ParserRegistry.get_parser_for_file``:
        # a parser whose ``supported_mime_types`` omits the detected type is
        # skipped outright, so ``score`` is never reached.  Inspecting the
        # archive here is therefore only possible by claiming the type first
        # and declining it below -- the low score keeps us out of the way of
        # any parser that genuinely handles ZIP files.
        if mime_type in _ZIP_MIME_TYPES and path is not None:
            if _looks_like_epub(path):
                return _ZIP_ALIAS_SCORE

        return None

    # -- Capability flags -------------------------------------------------

    @property
    def can_produce_archive(self) -> bool:
        """Whether :meth:`parse` can emit a searchable PDF archive copy.

        Always ``False``: this parser is text-only by design.  The consumer
        reads this to decide whether to ask for an archive PDF at all.
        """
        return False

    @property
    def requires_pdf_rendition(self) -> bool:
        """Whether a PDF must be generated for the frontend to display the file.

        Always ``False``.  A browser cannot render an EPUB natively, but the
        point of ingesting EPUBs here is search, not display -- the document is
        readable in its original form elsewhere.
        """
        return False

    # -- Lifecycle --------------------------------------------------------

    def configure(self, context: ParserContext) -> None:
        """Accept per-consumption context from the consumer.

        No-op: EPUB parsing needs nothing from ``ParserContext`` (it carries
        mail-rule and OCR settings relevant to other parsers).
        """

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        """Extract the document's text into :attr:`_text`.

        Parameters
        ----------
        document_path:
            Path to the EPUB working copy.
        mime_type:
            Detected MIME type (unused; the converter sniffs the file itself).
        produce_archive:
            Ignored.  Accepted for protocol parity -- ``can_produce_archive``
            is ``False``, so the consumer never requests one.

        Raises
        ------
        ParseError
            If the file cannot be opened or converted.  Raised as Paperless's
            own ``ParseError`` where that class is importable so the consumer's
            error path reports it verbatim; falls back to this module's
            :class:`ParseError` in a bare test environment.
        """
        try:
            text, title = _extract_epub(document_path)
        except Exception as exc:  # noqa: BLE001 - re-raised as ParseError below
            raise _parse_error(f"EPUB parse failed for {document_path}: {exc}") from exc

        if not text:
            raise _parse_error(
                f"EPUB parse produced no text for {document_path}. "
                "The file may be DRM-protected, corrupted, or contain only "
                "images (fixed-layout EPUBs have no extractable flow text).",
            )

        self._text = text
        self._title = title

    # -- Result accessors -------------------------------------------------

    def get_text(self) -> str:
        """Return the extracted Markdown text (empty string if absent)."""
        return self._text

    def get_date(self) -> datetime.datetime | None:
        """Return a publication date, or ``None`` to let Paperless guess.

        Deliberately ``None``: EPUB ``dc:date`` is unreliable in practice
        (often a build timestamp rather than a publication date, and absent
        entirely from the fixtures we validated against).  Paperless's own
        date parser reads the filename and extracted text and does a better
        job than a blind metadata grab would.
        """
        return None

    def get_archive_path(self) -> Path | None:
        """Return the archive PDF path.

        Always ``None`` -- no archive is produced (see
        :attr:`can_produce_archive`).
        """
        return None

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        """Return ``None``: EPUB is reflowable and has no meaningful page count."""
        return None

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        """Render extracted text onto a WebP thumbnail.

        Follows the built-in ``TextDocumentParser`` precedent rather than
        attempting cover-art extraction -- see the module docstring for why.

        ``__init__`` runs before ``parse()`` and creates the scratch directory,
        and ``get_thumbnail`` is called immediately after ``parse()``, so
        :attr:`_text` is populated here.  As a safety net for the edge case
        where the thumbnail is requested without a successful parse, the text
        is re-extracted on demand rather than raising -- a thumbnail failure
        would otherwise abort the whole consumption, since the consumer calls
        this unguarded.

        Parameters
        ----------
        document_path:
            Path to the EPUB working copy.
        mime_type:
            Detected MIME type (unused).

        Returns
        -------
        Path
            Path to ``thumb.webp`` inside this parser's scratch directory.
        """
        from PIL import Image
        from PIL import ImageDraw
        from PIL import ImageFont

        text = self._text
        if not text:
            try:
                text = _extract_epub(document_path)[0]
            except Exception:  # noqa: BLE001 - thumbnail must never abort
                text = ""

        preview = text[:_THUMB_TEXT_CHARS] if text else "(no extractable text)"

        image = Image.new("RGB", _THUMB_SIZE, color="white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=_THUMB_FONT_SIZE)
        except TypeError:  # Pillow < 10.1 has no ``size`` parameter
            font = ImageFont.load_default()
        draw.multiline_text(
            (_THUMB_PADDING, _THUMB_PADDING),
            preview,
            font=font,
            fill="black",
            spacing=_THUMB_SPACING,
        )

        out_path = self._tempdir / "thumb.webp"
        image.save(out_path, format="WEBP")
        return out_path

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        """Return metadata entries for the document.

        Not implemented for EPUB.  The Dash/Document-Intelligence metadata
        sidebar is driven by embedded format metadata (XMP, PDF info dicts),
        which EPUB does not carry in a form Paperless models.  The values
        ``markitdown`` does surface (title, authors, language) are already
        prepended to the extracted text, so they remain searchable.

        Returns
        -------
        list[MetadataEntry]
            Always empty.  Must never raise.
        """
        return []


# ---------------------------------------------------------------------------
# Conversion seam
# ---------------------------------------------------------------------------
# Isolated in a module-level function so tests can patch exactly one symbol.


def _extract_epub(path: Path) -> tuple[str, str | None]:
    """Convert an EPUB to Markdown text.

    Delegates to ``markitdown``'s ``EpubConverter``, which handles the format
    correctly for the overwhelming majority of books.  Before doing so it
    checks for the one known condition where that converter silently fails --
    a namespace-prefixed OPF, which defeats its qualified-name spine lookup --
    and takes the local spine-walking path instead.  See ``_epub.py`` for the
    full analysis.

    Isolated so the test suite can substitute a stub without importing
    ``markitdown``.

    Returns
    -------
    tuple[str, str | None]
        ``(markdown_text, title_or_None)``.  ``title`` is whatever the EPUB's
        Dublin Core metadata declared and is frequently ``None``.

    Raises
    ------
    ImportError
        If ``markitdown`` is not installed.
    Exception
        Whatever the converter raises (malformed archive, DRM, etc.).
    """
    # Imported lazily so the module remains importable -- and the plugin
    # class introspectable by the registry -- even if the optional converter
    # is missing.
    from markitdown import MarkItDown

    from ._epub import extract_text_via_spine
    from ._epub import spine_lookup_is_defeated

    if spine_lookup_is_defeated(path):
        return extract_text_via_spine(path)

    converter = MarkItDown()
    result = converter.convert(str(path))

    text = getattr(result, "text_content", None) or getattr(result, "markdown", None)
    if not isinstance(text, str):
        text = ""
    title = getattr(result, "title", None)
    return text, title


def _parse_error(message: str) -> Exception:
    """Build the most appropriate ``ParseError`` for the current environment.

    Prefers Paperless-ngx's own ``documents.parsers.ParseError`` -- the exact
    type the consumer's ``except ParseError`` clause catches, so failures are
    reported as document errors rather than "unexpected error".  Falls back to
    this module's class when Django/Paperless is not importable, which is the
    case in the standalone test environment.
    """
    try:
        from documents.parsers import ParseError as PaperlessParseError

        return PaperlessParseError(message)
    except Exception:  # noqa: BLE001 - Django not configured / not installed
        return ParseError(message)
