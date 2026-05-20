"""
srdom — Canonical Python library and CLI for querying SRDOM documents.

SRDOM is a deliberately structured HTML representation of the D&D SRD 5.2.1.
This library parses an SRDOM document and provides typed, indexed access to
its spells and creatures, plus a JSON-emitting CLI suited for pipeline use
(Nushell, jq, etc.).

QUICK START — LIBRARY
---------------------

    >>> import srdom
    >>> doc = srdom.load()                          # uses cache, fetches if absent
    >>> doc.spells["fireball"].casting_time
    'Action'
    >>> doc.spells["fireball"].description          # Markdown (default)
    'A bright streak flashes...'
    >>> doc = srdom.load(content="html")            # request raw HTML descriptions
    >>> doc.spells["fireball"].description
    '<p>A bright streak flashes...</p>...'

QUICK START — CLI
-----------------

    $ python srdom.py spells                        # all spells as JSON
    $ python srdom.py creatures                     # all creatures as JSON
    $ python srdom.py spell fireball                # one spell as JSON
    $ python srdom.py creature aboleth              # one creature as JSON
    $ python srdom.py spells --content html         # html-format descriptions
    $ python srdom.py spells --source refresh       # force redownload from URL
    $ python srdom.py test                          # run self-test

DEPENDENCIES
------------

Requires `lxml` (https://lxml.de/). No other third-party dependencies.
Everything else (HTTP, JSON, argparse, caching) is stdlib.

SOURCE RESOLUTION
-----------------

The `--source` flag (CLI) and `source=` argument (library) control which
SRDOM document is loaded, with these forms:

- `--source refresh`                  → force redownload from default URL
- `--source <filepath>`               → load from local file
- `--source <https://...>`            → load from arbitrary URL
- (omitted)                           → use cache; fetch from default URL if
                                         absent or stale (24h TTL)

The default URL is the canonical published location:
  https://srdom.sourcetrait.pub/srdom.html

CACHE LAYOUT
------------

Cached files live in an OS-appropriate cache directory (XDG_CACHE_HOME on
Linux, ~/Library/Caches on macOS, %LOCALAPPDATA% on Windows):

    <cache>/srdom/
      VERSION                                       # tracks latest version
      srdom_v<version>.html                         # source HTML
      srdom_v<version>_spells.json                  # md-format JSON cache
      srdom_v<version>_spells_html.json             # html-format JSON cache
      srdom_v<version>_creatures.json
      srdom_v<version>_creatures_html.json

The version string comes from the source's `<meta name="version">` tag.
Pre-release suffixes (e.g., `0.5.0-draft`) are preserved verbatim in the
cache filename, e.g., `srdom_v0.5.0-draft.html`.

CONTENT FORMATS
---------------

The `--content` flag (CLI) and `content=` argument (library) select the
serialization format for prose fields (`.description` and analogous fields
on effects, traits, actions, etc.):

- `md` (default)  → CommonMark Markdown; preserves paragraphs, lists,
                     emphasis (*italic*, **bold**), and basic tables
- `html`          → raw HTML fragment; full source fidelity

Each (content, type) combination is cached independently (md cache reused
across all md callers; html cache reused across all html callers). The md
cache is the default and uses an implicit suffix in the filename; the html
cache uses an explicit `_html` suffix.

DOM CONTRACT
------------

This library targets the SRDOM structural conventions:

- Spells: <section class="spell" id="spell-{slug}">
- Creatures: <section class="creature" id="creature-{slug}">
- Field values carry single prefixed classes: spell-cast-time, creature-ac, etc.
- Element types vary by content (td/span/dt/dd/h-tags); query by class, not by element.

For the full DOM contract with worked examples, see the Usage section at the
end of srdom.html (anchor #using-srdom).

CANONICAL URL
-------------

This library is published at: https://srdom.sourcetrait.pub/srdom.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    from lxml import html as _lhtml
    from lxml.etree import _Element
except ImportError as e:
    raise ImportError(
        "srdom requires lxml. Install via `pip install lxml`. "
        "If installation is unavailable, fall back to crafting regex queries "
        "against srdom.html directly (see #using-srdom for the structural "
        "conventions)."
    ) from e


__version__ = "0.2.0-draft"
__all__ = [
    "load",
    "Document",
    "Spell",
    "SpellCollection",
    "Creature",
    "CreatureCollection",
    "Effect",
    "Trait",
    "Action",
    "Special",
    "Components",
    "CR",
    "DEFAULT_URL",
]


# ============================================================================
# Module constants
# ============================================================================


DEFAULT_URL = "https://srdom.sourcetrait.pub/srdom.html"
TTL_SECONDS = 24 * 60 * 60  # 24 hours
_USER_AGENT = f"srdom-py/{__version__}"
_VERSION_META_RE = re.compile(
    rb'<meta\s+name="version"\s+content="([^"]+)"', re.IGNORECASE
)


# ============================================================================
# Cache management
# ============================================================================


def _cache_dir() -> Path:
    """Return the OS-appropriate cache directory for SRDOM artifacts."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "srdom"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "srdom" / "Cache"
        return Path.home() / "AppData" / "Local" / "srdom" / "Cache"
    # Linux / BSD / other Unix
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "srdom"
    return Path.home() / ".cache" / "srdom"


def _ensure_cache_dir() -> Path:
    """Create the cache directory if missing and return its path."""
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_cache_version() -> Optional[str]:
    """Read the version string from the VERSION file, or None if absent."""
    f = _cache_dir() / "VERSION"
    if not f.exists():
        return None
    return f.read_text(encoding="utf-8").strip() or None


def _write_cache_version(version: str) -> None:
    """Atomically write the version string to the VERSION file."""
    d = _ensure_cache_dir()
    tmp = d / "VERSION.tmp"
    tmp.write_text(version, encoding="utf-8")
    tmp.replace(d / "VERSION")


def _html_cache_path(version: str) -> Path:
    return _cache_dir() / f"srdom_v{version}.html"


def _json_cache_path(version: str, entity_type: str, content: str) -> Path:
    """Build the JSON cache filename for a (version, type, content) tuple.

    Implicit md: filename omits content suffix for md (the default).
    Explicit html: filename includes `_html` suffix.
    """
    base = f"srdom_v{version}_{entity_type}"
    if content == "html":
        base += "_html"
    return _cache_dir() / f"{base}.json"


def _extract_version(data: bytes) -> str:
    """Extract the version string from an SRDOM document's meta tag."""
    m = _VERSION_META_RE.search(data)
    if not m:
        raise ValueError(
            "Source HTML has no <meta name='version'> tag — not a valid SRDOM document?"
        )
    return m.group(1).decode("ascii")


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _fetch_url(url: str) -> bytes:
    """Fetch a URL and return its body as bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _is_cache_fresh(path: Path) -> bool:
    """True if the cached file exists and is younger than TTL_SECONDS."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age <= TTL_SECONDS


def _resolve_source(source: Optional[str]) -> Tuple[Path, str]:
    """Resolve the source argument to a (file_path, version) tuple.

    Source semantics:
    - None / "" → use cache; fetch from DEFAULT_URL if absent or stale
    - "refresh" → force fetch from DEFAULT_URL
    - URL string → fetch from the given URL (and cache it)
    - filepath → use the given local file (not cached)
    """
    # Explicit local path
    if source and not _is_url(source) and source != "refresh":
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"No such file: {source}")
        data = p.read_bytes()
        version = _extract_version(data)
        return (p, version)

    # Explicit URL or refresh
    if source == "refresh":
        return _fetch_and_cache(DEFAULT_URL)
    if source and _is_url(source):
        return _fetch_and_cache(source)

    # Default: use cache if fresh, else fetch
    cached_version = _read_cache_version()
    if cached_version:
        cache_path = _html_cache_path(cached_version)
        if _is_cache_fresh(cache_path):
            return (cache_path, cached_version)
    return _fetch_and_cache(DEFAULT_URL)


def _fetch_and_cache(url: str) -> Tuple[Path, str]:
    """Fetch a URL, cache it as srdom_v<version>.html, update VERSION."""
    data = _fetch_url(url)
    version = _extract_version(data)
    _ensure_cache_dir()
    cache_path = _html_cache_path(version)
    cache_path.write_bytes(data)
    _write_cache_version(version)
    return (cache_path, version)


# ============================================================================
# HTML → Markdown conversion
# ============================================================================


def _md_inline(element: _Element) -> str:
    """Convert an element's children to inline Markdown (em, strong, code, etc.)."""
    out: List[str] = []
    if element.text:
        out.append(element.text)
    for child in element:
        tag = child.tag
        inner = _md_inline(child)
        if tag == "em" or tag == "i":
            out.append(f"*{inner}*")
        elif tag == "strong" or tag == "b":
            out.append(f"**{inner}**")
        elif tag == "code":
            out.append(f"`{inner}`")
        elif tag == "br":
            out.append("  \n")
        elif tag == "a":
            href = child.get("href", "")
            out.append(f"[{inner}]({href})" if href else inner)
        else:
            # Transparent passthrough for span and other unknown inline tags
            out.append(inner)
        if child.tail:
            out.append(child.tail)
    return "".join(out)


def _md_table(element: _Element) -> str:
    """Convert a simple HTML table to Markdown. Fall back to raw HTML for
    tables with non-uniform row widths."""
    rows: List[List[str]] = []
    for tr in element.iter("tr"):
        cells = []
        for cell in tr:
            if cell.tag in ("td", "th"):
                txt = _md_inline(cell).strip()
                txt = txt.replace("\n", " ").replace("|", "\\|")
                cells.append(txt)
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = len(rows[0])
    if not all(len(r) == width for r in rows):
        # Non-uniform; bail back to HTML so renderers can show it accurately.
        return _lhtml.tostring(element, encoding="unicode").strip()
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _md_block(element: _Element) -> str:
    """Convert a block-level element to Markdown."""
    tag = element.tag
    if tag == "p":
        return _md_inline(element).strip()
    if tag == "ul":
        items = [f"- {_md_inline(li).strip()}" for li in element.findall("li")]
        return "\n".join(items)
    if tag == "ol":
        items = [
            f"{i}. {_md_inline(li).strip()}"
            for i, li in enumerate(element.findall("li"), start=1)
        ]
        return "\n".join(items)
    if tag == "table":
        return _md_table(element)
    if tag in ("div", "section"):
        # Container: recurse into children as block-level
        return _md_children(element)
    # Fallback: take text content
    return element.text_content().strip()


def _md_children(element: _Element) -> str:
    """Convert all block-level children of an element to Markdown."""
    parts = []
    for child in element:
        rendered = _md_block(child)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts).strip()


def _html_fragment(element: _Element) -> str:
    """Return the HTML of an element's children, joined into one string."""
    return "".join(
        _lhtml.tostring(c, encoding="unicode") for c in element
    ).strip()


# ============================================================================
# Module-level entry point
# ============================================================================


def load(source: Optional[str] = None, content: str = "md") -> "Document":
    """Load an SRDOM document.

    Args:
        source: Source resolution directive (see module docstring):
                - None (default): use cache, fetch DEFAULT_URL if absent/stale
                - "refresh": force fetch from DEFAULT_URL
                - URL: fetch from given URL
                - filepath: load from local file
        content: Format for `.description` and analogous prose fields:
                 - "md" (default): CommonMark Markdown
                 - "html": raw HTML fragment

    Returns:
        A Document instance with indexed access to spells and creatures.

    Example:
        >>> doc = srdom.load()
        >>> len(doc.spells)
        339
        >>> doc.spells["fireball"].school
        'Evocation'
    """
    if content not in ("md", "html"):
        raise ValueError(f"content must be 'md' or 'html', got {content!r}")
    path, version = _resolve_source(source)
    with open(path, "rb") as f:
        tree = _lhtml.fromstring(f.read())
    return Document(tree, content=content, version=version, path=path)


# ============================================================================
# Document
# ============================================================================


class Document:
    """A parsed SRDOM document.

    Provides indexed access to the document's spells and creatures.

    Attributes:
        spells: SpellCollection
        creatures: CreatureCollection
        version: document version string
        content: requested content format ("md" or "html")

    Example:
        >>> doc = srdom.load()
        >>> doc.spells["fireball"]
        <Spell 'fireball': Level 3 Evocation>
        >>> doc.creatures["aboleth"]
        <Creature 'aboleth': Large Aberration, CR 10>
    """

    def __init__(
        self,
        tree: _Element,
        content: str = "md",
        version: Optional[str] = None,
        path: Optional[Path] = None,
    ):
        self._tree = tree
        self._content = content
        self._version = version or self._read_version_from_tree()
        self._path = path
        self._spells = SpellCollection(tree, content)
        self._creatures = CreatureCollection(tree, content)

    def _read_version_from_tree(self) -> Optional[str]:
        result = self._tree.xpath('//meta[@name="version"]/@content')
        return result[0] if result else None

    @property
    def spells(self) -> "SpellCollection":
        return self._spells

    @property
    def creatures(self) -> "CreatureCollection":
        return self._creatures

    @property
    def version(self) -> Optional[str]:
        return self._version

    @property
    def content(self) -> str:
        return self._content

    @property
    def element(self) -> _Element:
        """Underlying lxml root element."""
        return self._tree

    def __repr__(self) -> str:
        return (
            f"<Document version={self._version!r} content={self._content!r} "
            f"spells={len(self._spells)} creatures={len(self._creatures)}>"
        )


# ============================================================================
# Collections
# ============================================================================


class _BaseCollection:
    _entity_class = None  # set after subclass definitions
    _id_prefix = None
    _section_class = None

    def __init__(self, tree: _Element, content: str = "md"):
        self._tree = tree
        self._content = content

    def __getitem__(self, slug: str):
        result = self._tree.xpath(f'id("{self._id_prefix}{slug}")')
        if not result:
            raise KeyError(slug)
        return self._entity_class(result[0], content=self._content)

    def __iter__(self):
        for el in self._tree.xpath(f'//section[@class="{self._section_class}"]'):
            yield self._entity_class(el, content=self._content)

    def __len__(self) -> int:
        return len(self._tree.xpath(f'//section[@class="{self._section_class}"]'))

    def __contains__(self, slug: str) -> bool:
        return bool(self._tree.xpath(f'id("{self._id_prefix}{slug}")'))

    def filter(self, **kwargs) -> Iterator:
        """Filter entities by field values.

        Suffix operators:
            field=value         exact match
            field__ne=value     not equal
            field__lt=value     less than
            field__lte=value    less than or equal
            field__gt=value     greater than
            field__gte=value    greater than or equal
            field__in=iterable  membership in iterable
            field__contains=x   x is in the field (string or list)
        """
        for entity in self:
            if all(_match(entity, k, v) for k, v in kwargs.items()):
                yield entity


def _match(entity, key: str, expected) -> bool:
    op = "eq"
    field = key
    if "__" in key:
        field, op = key.rsplit("__", 1)
    actual = getattr(entity, field, None)
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "lt":
        return actual is not None and actual < expected
    if op == "lte":
        return actual is not None and actual <= expected
    if op == "gt":
        return actual is not None and actual > expected
    if op == "gte":
        return actual is not None and actual >= expected
    if op == "in":
        return actual in expected
    if op == "contains":
        if actual is None:
            return False
        return expected in actual
    raise ValueError(f"Unknown filter operator: {op}")


class SpellCollection(_BaseCollection):
    """All 339 spells in the document. Indexable by slug, iterable, filterable."""
    _section_class = "spell"
    _id_prefix = "spell-"


class CreatureCollection(_BaseCollection):
    """All creatures in the document. Indexable by slug, iterable, filterable.

    Includes monsters (in the Monsters A-Z section), animals (in the Animals
    section), and creatures embedded in spells and magic items. Use the
    `.category` field to distinguish them.
    """
    _section_class = "creature"
    _id_prefix = "creature-"


# ============================================================================
# Value types
# ============================================================================


@dataclass(frozen=True)
class Components:
    """Parsed spell components.

    Attributes:
        verbal: spell requires verbal components
        somatic: spell requires somatic components
        material: material description, or None if no material component
    """
    verbal: bool
    somatic: bool
    material: Optional[str]

    @classmethod
    def parse(cls, raw: str) -> "Components":
        verbal = "Verbal" in raw
        somatic = "Somatic" in raw
        material = None
        if "Material" in raw:
            m = re.search(r"Material\s*\(([^)]+)\)", raw)
            material = m.group(1) if m else ""
        return cls(verbal=verbal, somatic=somatic, material=material)

    def to_dict(self) -> dict:
        return {
            "verbal": self.verbal,
            "somatic": self.somatic,
            "material": self.material,
        }


@dataclass(frozen=True)
class CR:
    """Challenge Rating, supporting fractional and integer values.

    Comparison is numeric: CR("1/4") < CR("1/2") < CR(1) < CR(10).
    Equality is numeric: CR("2/4") == CR("1/2").

    Attributes:
        numerator, denominator: rational form (denominator=1 for whole numbers)
    """
    numerator: int
    denominator: int = 1

    @classmethod
    def parse(cls, raw: str) -> "CR":
        raw = raw.strip()
        if "/" in raw:
            num, denom = raw.split("/", 1)
            return cls(numerator=int(num), denominator=int(denom))
        return cls(numerator=int(raw), denominator=1)

    def __str__(self) -> str:
        if self.denominator == 1:
            return str(self.numerator)
        return f"{self.numerator}/{self.denominator}"

    def __repr__(self) -> str:
        return f"CR({self})"

    @property
    def numeric(self) -> float:
        return self.numerator / self.denominator

    def __lt__(self, other: "CR") -> bool:
        return self.numeric < other.numeric

    def __le__(self, other: "CR") -> bool:
        return self.numeric <= other.numeric

    def __gt__(self, other: "CR") -> bool:
        return self.numeric > other.numeric

    def __ge__(self, other: "CR") -> bool:
        return self.numeric >= other.numeric

    def __eq__(self, other) -> bool:
        if not isinstance(other, CR):
            return NotImplemented
        return self.numeric == other.numeric

    def __hash__(self) -> int:
        return hash(self.numeric)


# ============================================================================
# Named-item types (effects, traits, actions)
# ============================================================================


class _NamedItem:
    """Common shape for named items: spell effects, creature traits, actions, etc."""

    def __init__(self, dt: _Element, dd: _Element, content: str = "md"):
        self._dt = dt
        self._dd = dd
        self._content = content

    @property
    def name(self) -> str:
        return self._dt.text_content().strip()

    @property
    def slug(self) -> str:
        full_id = self._dd.get("id", "")
        parts = full_id.split("-")
        for marker in ("effect", "trait", "action", "reaction"):
            if marker in parts:
                idx = parts.index(marker)
                return "-".join(parts[idx + 1:])
        return full_id

    @property
    def description(self) -> str:
        if self._content == "html":
            return _html_fragment(self._dd)
        return _md_children(self._dd)

    @property
    def constraint(self) -> Optional[str]:
        result = self._dd.xpath('.//span[@class="constraint"]/text()')
        return result[0] if result else None

    @property
    def element(self) -> _Element:
        return self._dd

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "constraint": self.constraint,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


class Effect(_NamedItem):
    """A named effect on a spell or creature trait."""


class Trait(_NamedItem):
    """A named creature trait."""


class Action(_NamedItem):
    """A named creature action, bonus action, reaction, or legendary action."""


@dataclass(frozen=True)
class Special:
    """An embedded reference sub-section in a spell (e.g., Control Weather's
    Precipitation table)."""
    heading: str
    slug: str
    content: str

    def to_dict(self) -> dict:
        return {"heading": self.heading, "slug": self.slug, "content": self.content}


# ============================================================================
# Spell
# ============================================================================


class Spell:
    """A spell entry in SRDOM.

    See module docstring for the full attribute list.
    """

    def __init__(self, element: _Element, content: str = "md"):
        self._element = element
        self._content = content

    @property
    def element(self) -> _Element:
        return self._element

    @property
    def slug(self) -> str:
        return self._element.get("id", "").removeprefix("spell-")

    @property
    def name(self) -> str:
        return self._element.xpath('./h4[@class="spell-name"]/text()')[0]

    @property
    def level(self) -> Optional[int]:
        result = self._element.xpath(
            './p[@class="spell-general"]/span[@class="spell-level"]/text()'
        )
        return int(result[0]) if result else None

    @property
    def upgrade(self) -> Optional[str]:
        result = self._element.xpath(
            './p[@class="spell-general"]/span[@class="spell-upgrade"]/text()'
        )
        return result[0] if result else None

    @property
    def school(self) -> str:
        return self._element.xpath(
            './p[@class="spell-general"]/span[@class="spell-school"]/text()'
        )[0]

    @property
    def classes(self) -> List[str]:
        return [c.strip() for c in self.classes_raw.split(",")]

    @property
    def classes_raw(self) -> str:
        return self._element.xpath(
            './p[@class="spell-general"]/span[@class="spell-classes"]/text()'
        )[0]

    @property
    def casting_time(self) -> str:
        return self._element.xpath(
            './table[@class="spell-cast"]/tr/td[@class="spell-cast-time"]/text()'
        )[0]

    @property
    def range(self) -> str:
        return self._element.xpath(
            './table[@class="spell-cast"]/tr/td[@class="spell-cast-range"]/text()'
        )[0]

    @property
    def components(self) -> Components:
        return Components.parse(self.components_raw)

    @property
    def components_raw(self) -> str:
        return self._element.xpath(
            './table[@class="spell-cast"]/tr/td[@class="spell-cast-components"]/text()'
        )[0]

    @property
    def duration(self) -> str:
        return self._element.xpath(
            './table[@class="spell-cast"]/tr/td[@class="spell-cast-duration"]/text()'
        )[0]

    @property
    def description(self) -> str:
        descs = self._element.xpath('./div[@class="spell-description"]')
        if not descs:
            return ""
        if self._content == "html":
            return _html_fragment(descs[0])
        return _md_children(descs[0])

    @property
    def effects(self) -> List[Effect]:
        dl = self._element.xpath('./dl[@class="spell-effects"]')
        if not dl:
            return []
        dl = dl[0]
        dts = dl.xpath("./dt")
        dds = dl.xpath("./dd")
        return [Effect(dt, dd, content=self._content) for dt, dd in zip(dts, dds)]

    @property
    def specials(self) -> List[Special]:
        result = []
        for sec in self._element.xpath(
            './div[@class="spell-specials"]/section[@class="spell-special"]'
        ):
            heading_text = sec.xpath("./h5/text() | ./h6/text()")
            heading = heading_text[0] if heading_text else ""
            full_id = sec.get("id", "")
            parts = full_id.split("-special-")
            slug = parts[1] if len(parts) == 2 else full_id
            if self._content == "html":
                body = _html_fragment(sec)
            else:
                body = _md_children(sec)
            result.append(Special(heading=heading, slug=slug, content=body))
        return result

    @property
    def creature(self) -> Optional["Creature"]:
        result = self._element.xpath('./section[@class="creature"]')
        return Creature(result[0], content=self._content) if result else None

    def to_dict(self) -> dict:
        embedded = self.creature
        return {
            "slug": self.slug,
            "name": self.name,
            "level": self.level,
            "upgrade": self.upgrade,
            "school": self.school,
            "classes": self.classes,
            "casting_time": self.casting_time,
            "range": self.range,
            "components": self.components.to_dict(),
            "components_raw": self.components_raw,
            "duration": self.duration,
            "description": self.description,
            "effects": [e.to_dict() for e in self.effects],
            "specials": [s.to_dict() for s in self.specials],
            "creature": embedded.to_dict() if embedded is not None else None,
        }

    def __repr__(self) -> str:
        if self.level is not None:
            return f"<Spell {self.slug!r}: Level {self.level} {self.school}>"
        return f"<Spell {self.slug!r}: {self.school} Cantrip>"


# ============================================================================
# Creature
# ============================================================================


_CREATURE_CATEGORY_HEADINGS = {
    "monsters-a-z": "monster",
    "animals": "animal",
}


class Creature:
    """A creature stat block in SRDOM.

    See module docstring for the full attribute list. The `.category` field
    distinguishes monsters (in Monsters A-Z) from animals (in Animals) from
    embedded creatures (inside spells or magic-item descriptions).
    """

    _ABILITIES = (
        "strength", "dexterity", "constitution",
        "intelligence", "wisdom", "charisma",
    )
    _OPTIONAL_DETAILS = (
        "skills", "immunities", "resistances", "gear", "vulnerabilities",
    )

    def __init__(self, element: _Element, content: str = "md"):
        self._element = element
        self._content = content

    @property
    def element(self) -> _Element:
        return self._element

    @property
    def slug(self) -> str:
        return self._element.get("id", "").removeprefix("creature-")

    @property
    def name(self) -> str:
        result = self._element.xpath('./h3[@class="creature-name"]/text()')
        if not result:
            result = self._element.xpath('./h5[@class="creature-name"]/text()')
        return result[0] if result else ""

    @property
    def category(self) -> str:
        """One of: "monster", "animal", "embedded-spell", "embedded-magic-item",
        or "unknown" if the parent context can't be determined.

        Derived from the nearest preceding-sibling <h2> heading id, or by
        walking ancestors when the creature is nested inside another section
        (e.g., a spell's embedded summon).
        """
        # First, check if this creature is nested inside another section
        # (e.g., a spell or magic item)
        ancestors = self._element.xpath('ancestor::section')
        for anc in reversed(ancestors):
            cls = anc.get("class") or ""
            if "spell" in cls.split():
                return "embedded-spell"
            if "magic-item" in cls.split():
                return "embedded-magic-item"
        # Otherwise, find the nearest preceding-sibling h2
        h2s = self._element.xpath('preceding-sibling::h2[1]')
        if h2s:
            h2_id = h2s[0].get("id", "")
            if h2_id in _CREATURE_CATEGORY_HEADINGS:
                return _CREATURE_CATEGORY_HEADINGS[h2_id]
            if h2_id in ("magic-items-a-z", "magic-items-2", "magic-items"):
                return "embedded-magic-item"
        return "unknown"

    # ---- Type line ------------------------------------------------------

    @property
    def size(self) -> str:
        return self._first_text('.//span[@class="creature-size"]/text()')

    @property
    def type(self) -> str:
        return self._first_text('.//span[@class="creature-type"]/text()')

    @property
    def alignment(self) -> str:
        return self._first_text('.//span[@class="creature-alignment"]/text()')

    # ---- Combat highlights ---------------------------------------------

    @property
    def ac(self) -> str:
        return self._first_text('.//td[@class="creature-ac"]/text()')

    @property
    def hp(self) -> str:
        return self._first_text('.//td[@class="creature-hp"]/text()')

    @property
    def speed(self) -> str:
        return self._first_text('.//td[@class="creature-speed"]/text()')

    @property
    def initiative(self) -> Optional[str]:
        return self._optional_text('.//td[@class="creature-initiative"]/text()')

    # ---- Abilities (dynamic via __getattr__) ---------------------------

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        for ability in self._ABILITIES:
            if name == ability:
                raw = self._first_text(
                    f'.//td[@class="creature-{ability}"]/text()'
                )
                return int(raw) if raw else None
            if name == f"{ability}_modifier":
                return self._first_text(
                    f'.//td[@class="creature-{ability}-modifier"]/text()'
                )
            if name == f"{ability}_save":
                return self._first_text(
                    f'.//td[@class="creature-{ability}-save"]/text()'
                )
        if name in self._OPTIONAL_DETAILS:
            return self._optional_text(f'.//td[@class="creature-{name}"]/text()')
        raise AttributeError(
            f"{type(self).__name__!r} has no attribute {name!r}"
        )

    # ---- Details -------------------------------------------------------

    @property
    def senses(self) -> str:
        return self._first_text('.//td[@class="creature-senses"]/text()')

    @property
    def languages(self) -> str:
        return self._first_text('.//td[@class="creature-languages"]/text()')

    @property
    def cr(self) -> Optional[CR]:
        raw = self._first_text('.//td[@class="creature-cr"]/text()')
        raw = raw.split("(")[0].strip()
        if not raw or raw.lower() == "none":
            return None
        try:
            return CR.parse(raw)
        except ValueError:
            return None

    @property
    def cr_raw(self) -> str:
        return self._first_text('.//td[@class="creature-cr"]/text()')

    # ---- Stat-block subsections ----------------------------------------

    @property
    def traits(self) -> List[Trait]:
        return self._named_items("creature-traits", Trait)

    @property
    def actions(self) -> List[Action]:
        return self._named_items("creature-actions", Action)

    @property
    def bonus_actions(self) -> List[Action]:
        return self._named_items("creature-bonus-actions", Action)

    @property
    def reactions(self) -> List[Action]:
        return self._named_items("creature-reactions", Action)

    @property
    def legendary_actions(self) -> List[Action]:
        return self._named_items("creature-legendary-actions", Action)

    # ---- Helpers -------------------------------------------------------

    def _first_text(self, xpath_expr: str) -> str:
        result = self._element.xpath(xpath_expr)
        return result[0] if result else ""

    def _optional_text(self, xpath_expr: str) -> Optional[str]:
        result = self._element.xpath(xpath_expr)
        return result[0] if result else None

    def _named_items(self, section_class: str, item_class) -> List:
        sections = self._element.xpath(
            f'./section[@class="{section_class}"]/dl'
        )
        if not sections:
            return []
        dl = sections[0]
        dts = dl.xpath("./dt")
        dds = dl.xpath("./dd")
        return [
            item_class(dt, dd, content=self._content)
            for dt, dd in zip(dts, dds)
        ]

    # ---- Serialization -------------------------------------------------

    def to_dict(self) -> dict:
        d = {
            "slug": self.slug,
            "name": self.name,
            "category": self.category,
            "size": self.size,
            "type": self.type,
            "alignment": self.alignment,
            "ac": self.ac,
            "hp": self.hp,
            "speed": self.speed,
            "initiative": self.initiative,
        }
        for ability in self._ABILITIES:
            d[ability] = getattr(self, ability)
            d[f"{ability}_modifier"] = getattr(self, f"{ability}_modifier")
            d[f"{ability}_save"] = getattr(self, f"{ability}_save")
        d.update({
            "senses": self.senses,
            "languages": self.languages,
            "cr": str(self.cr) if self.cr is not None else None,
            "cr_numeric": self.cr.numeric if self.cr is not None else None,
            "cr_raw": self.cr_raw,
        })
        for detail in self._OPTIONAL_DETAILS:
            d[detail] = getattr(self, detail)
        d.update({
            "traits": [t.to_dict() for t in self.traits],
            "actions": [a.to_dict() for a in self.actions],
            "bonus_actions": [a.to_dict() for a in self.bonus_actions],
            "reactions": [a.to_dict() for a in self.reactions],
            "legendary_actions": [a.to_dict() for a in self.legendary_actions],
        })
        return d

    def __repr__(self) -> str:
        try:
            cr_str = f"CR {self.cr}" if self.cr is not None else "CR none"
            return f"<Creature {self.slug!r}: {self.size} {self.type}, {cr_str}>"
        except (IndexError, ValueError):
            return f"<Creature {self.slug!r}>"


# Forward-reference resolution
SpellCollection._entity_class = Spell
CreatureCollection._entity_class = Creature


# ============================================================================
# JSON cache helpers
# ============================================================================


def _serialize_collection(collection, content: str) -> list:
    """Serialize all entities in a collection to a list of dicts."""
    return [e.to_dict() for e in collection]


def _read_or_build_json_cache(
    version: str, entity_type: str, content: str, doc: "Document"
) -> list:
    """Read a JSON cache file if present; otherwise build it from doc and write."""
    cache_path = _json_cache_path(version, entity_type, content)
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Build
    if entity_type == "spells":
        data = _serialize_collection(doc.spells, content)
    elif entity_type == "creatures":
        data = _serialize_collection(doc.creatures, content)
    else:
        raise ValueError(f"Unknown entity_type: {entity_type!r}")
    _ensure_cache_dir()
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


# ============================================================================
# CLI
# ============================================================================


def _cmd_collection(args, entity_type: str) -> int:
    """Handle `spells` and `creatures` subcommands."""
    doc = load(source=args.source, content=args.content)
    data = _read_or_build_json_cache(
        doc.version, entity_type, args.content, doc
    )
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_entity(args, entity_type: str) -> int:
    """Handle `spell <slug>` and `creature <slug>` subcommands.

    Reads from JSON cache if present (and the slug exists in it); otherwise
    parses the source and writes the entity directly.
    """
    doc = load(source=args.source, content=args.content)
    collection = doc.spells if entity_type == "spells" else doc.creatures
    if args.slug not in collection:
        sys.stderr.write(f"No {entity_type[:-1]} with slug: {args.slug!r}\n")
        return 1
    entity = collection[args.slug]
    json.dump(entity.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_test(args) -> int:
    """Run a quick self-test against the resolved source."""
    return _self_test(source=args.source, content=args.content)


def _self_test(source: Optional[str] = None, content: str = "md") -> int:
    print(f"Loading source (source={source!r}, content={content!r})...")
    doc = load(source=source, content=content)
    print(f"  Version: {doc.version}")
    print(f"  Path:    {doc._path}")
    print(f"  Spells:  {len(doc.spells)}")
    print(f"  Creatures: {len(doc.creatures)}")

    print("\n--- Spell spot checks ---")
    for slug in ["acid-arrow", "fireball", "wish", "acid-splash"]:
        s = doc.spells[slug]
        print(f"  {s}: components={s.components}")

    print("\n--- Creature spot checks ---")
    for slug in ["aboleth", "ancient-red-dragon", "goblin-warrior", "commoner"]:
        try:
            c = doc.creatures[slug]
            print(f"  {c}: category={c.category}, STR={c.strength}")
        except KeyError:
            print(f"  (no creature: {slug})")

    print("\n--- Description format check ---")
    fb = doc.spells["fireball"]
    desc = fb.description
    if content == "md":
        print(f"  Fireball description (md, first 120 chars): {desc[:120]!r}")
        assert "**" in desc or "*" in desc or "-" in desc or len(desc) > 100, (
            "expected some markdown structure"
        )
    else:
        print(f"  Fireball description (html, first 120 chars): {desc[:120]!r}")
        assert desc.startswith("<"), "expected html fragment"

    print("\n--- Filter spot check ---")
    cantrips = list(doc.spells.filter(level=None))
    print(f"  Cantrips: {len(cantrips)}")
    monsters = [c for c in doc.creatures if c.category == "monster"]
    print(f"  Monsters: {len(monsters)}")
    monster_str10 = [c.name for c in monsters if c.strength == 10]
    print(f"  Monsters with STR 10: {len(monster_str10)}")
    for n in sorted(monster_str10)[:5]:
        print(f"    - {n}")

    print("\n--- CR comparison ---")
    print(f"  CR('1/4') < CR(1): {CR.parse('1/4') < CR.parse('1')}")

    print("\nSelf-test completed.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srdom",
        description=(
            "Query SRDOM (D&D SRD 5.2.1 as structured HTML). Emits JSON to stdout "
            "for pipeline use."
        ),
    )
    p.add_argument(
        "--source",
        default=None,
        help=(
            "Source resolution: 'refresh' (force redownload), a filepath, "
            "or a URL. Default: cache, or fetch from "
            f"{DEFAULT_URL} if absent/stale."
        ),
    )
    p.add_argument(
        "--content",
        choices=["md", "html"],
        default="md",
        help="Format for description fields. Default: md.",
    )

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("spells", help="Emit all spells as JSON.")
    sub.add_parser("creatures", help="Emit all creatures as JSON.")
    sp = sub.add_parser("spell", help="Emit one spell as JSON.")
    sp.add_argument("slug")
    sc = sub.add_parser("creature", help="Emit one creature as JSON.")
    sc.add_argument("slug")
    sub.add_parser("test", help="Run a self-test against the resolved source.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "spells":
        return _cmd_collection(args, "spells")
    if args.command == "creatures":
        return _cmd_collection(args, "creatures")
    if args.command == "spell":
        return _cmd_entity(args, "spells")
    if args.command == "creature":
        return _cmd_entity(args, "creatures")
    if args.command == "test":
        return _cmd_test(args)
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
