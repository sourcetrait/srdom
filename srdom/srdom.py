"""
srdom — query SRDOM (D&D SRD 5.2.1 as structured HTML).

Library and CLI for parsing and querying the SRDOM document. Organized in
four conceptual namespaces:

- (dommf): the DOMMF contract defines the data model; in Python it maps
   to dataclasses in the `model` namespace.
- domqf:  query helpers and reusable XPath primitives for walking the DOM.
- model:  pure dataclasses mirroring the DOMMF contract (Creature, Spell,
          MagicItem, Trait, Action, etc.).
- query:  runtime DOM-walking classes that produce model instances via
          to_model(), or plain dicts via to_dict() for JSON output.

Note: a few reaction.trigger / reaction.response and legendary_action.uses /
legendary_action.situation_uses fields return placeholder values pending HTML
markup work. Tests in `tests.test_TODO_*` assert the placeholders so they fail
visibly when the markup lands.

Run:
    python3 srdom.py spells | jq '.[0]'
    python3 srdom.py creature aboleth | jq '.actions'
    python3 srdom.py magic-item holy-avenger
    python3 srdom.py test
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

try:
    from lxml import etree as _letree
    from lxml import html as _lhtml
    from lxml.etree import _Element
except ImportError as e:
    raise ImportError("srdom requires lxml") from e


__version__ = "0.4.0"
DEFAULT_URL = "https://srdom.sourcetrait.pub/srdom.html"
TTL_SECONDS = 24 * 60 * 60
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


def _json_cache_path(version: str, entity_type: str) -> Path:
    """Build the JSON cache filename for a (version, type) tuple."""
    return _cache_dir() / f"srdom_v{version}_{entity_type}.json"


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



class domqf:
    """DOM Querying Format primitives - reusable across any DOMQF spec."""

    @staticmethod
    def first_text(element: _Element, xpath: str) -> str:
        r = element.xpath(xpath)
        return r[0].strip() if r else ""

    @staticmethod
    def all_text(element: _Element, xpath: str) -> List[str]:
        return [s.strip() for s in element.xpath(xpath) if str(s).strip()]

    @staticmethod
    def resolve_reference(
        element: _Element,
        attr: str,
        *,
        join: Optional[str] = None,
    ) -> str:
        """Resolve a data-*-id(s) attribute via document-wide ID lookup.

        If `join` is None, the attribute holds a single id; returns the text
        of the resolved element. If `join` is set, the attribute holds a CSV
        of ids; returns the resolved texts joined by `join`.
        """
        refs = element.get(attr, "")
        if not refs:
            return ""
        root = element.getroottree().getroot()
        if join is None:
            ref_id = refs.strip()
            r = root.xpath(f'//*[@id="{ref_id}"]/text()')
            return r[0].strip() if r else ""
        parts = []
        for ref_id in refs.split(","):
            ref_id = ref_id.strip()
            if not ref_id:
                continue
            r = root.xpath(f'//*[@id="{ref_id}"]/text()')
            if r:
                parts.append(r[0].strip())
        return join.join(parts)


class model:
    """SRDOM dataclass mirrors of the DOMMF contract."""

    class Situation(Enum):
        normal = "normal"
        in_lair = "in_lair"

    @dataclass
    class Trait:
        slug: str
        name: str
        description: str
        constraints: List[str] = field(default_factory=list)

    @dataclass
    class Action:
        slug: str
        name: str
        usage: str
        constraints: List[str] = field(default_factory=list)

    @dataclass
    class LegendaryAction:
        slug: str
        name: str
        uses: int
        usage: str
        situation_uses: dict = field(default_factory=dict)
        constraints: List[str] = field(default_factory=list)

    @dataclass
    class Reaction:
        slug: str
        name: str
        trigger: str
        response: str
        constraints: List[str] = field(default_factory=list)

    @dataclass
    class Creature:
        slug: str
        name: str
        size: str
        kind: str
        alignment: str
        armor_class: str
        initiative: str
        hit_points: str
        speed: str
        strength: int
        strength_modifier: int
        strength_save: int
        dexterity: int
        dexterity_modifier: int
        dexterity_save: int
        constitution: int
        constitution_modifier: int
        constitution_save: int
        intelligence: int
        intelligence_modifier: int
        intelligence_save: int
        wisdom: int
        wisdom_modifier: int
        wisdom_save: int
        charisma: int
        charisma_modifier: int
        charisma_save: int
        skills: str
        senses: str
        languages: str
        challenge_rating: str
        traits: list = field(default_factory=list)
        actions: list = field(default_factory=list)
        bonus_actions: list = field(default_factory=list)
        reactions: list = field(default_factory=list)
        legendary_actions: list = field(default_factory=list)

    @dataclass
    class SpellComponents:
        verbal: bool
        somatic: bool
        material: Optional[str] = None

    @dataclass
    class SpellEffect:
        slug: str
        name: str
        description: str
        constraints: List[str] = field(default_factory=list)

    @dataclass
    class SpecialRules:
        slug: str
        heading: str
        content: str

    @dataclass
    class Spell:
        slug: str
        name: str
        level: int  # 0 for cantrips, 1-9 for leveled spells
        school: str
        classes: List[str]
        casting_time: str
        range: str
        components: "model.SpellComponents"
        duration: str
        description: str
        upgrade: Optional[str] = None
        effects: list = field(default_factory=list)
        special_rules: list = field(default_factory=list)
        creature: Optional["model.Creature"] = None

    @dataclass
    class MagicItemRarityTier:
        rarity: str
        variant: Optional[str] = None

    @dataclass
    class MagicItem:
        slug: str
        title: str
        name: str
        variants: List[str]
        category: str
        rarities: List[str]
        rarity_tiers: list
        description: str
        category_description: Optional[str] = None
        attunement: Optional[str] = None
        special_rules: list = field(default_factory=list)
        creature: Optional["model.Creature"] = None




# ============================================================================
# query - SRDOM query-backed wrappers (instances of DOMQF queries)
# ============================================================================


class query:
    """SRDOM wrappers over lxml elements (DOMQF implementation)."""

    class _Characteristic:
        """Base for trait/action/reaction/legendary_action wrappers."""

        _XP_CONSTRAINTS = _letree.XPath('.//span[@class="constraint"]/text()')
        _MARKERS = ("effect", "trait", "action", "reaction")

        def __init__(self, dt: _Element, dd: _Element):
            self._dt = dt
            self._dd = dd

        @property
        def slug(self) -> str:
            full_id = self._dd.get("id", "")
            parts = full_id.split("-")
            for marker in self._MARKERS:
                if marker in parts:
                    return "-".join(parts[parts.index(marker) + 1:])
            return full_id

        @property
        def name(self) -> str:
            return self._dt.text_content().strip()

        @property
        def constraints(self) -> List[str]:
            return list(self._XP_CONSTRAINTS(self._dd))

        def _md(self) -> str:
            """Markdown rendering of this characteristic's content body."""
            return _md_children(self._dd)

        def __repr__(self) -> str:
            return f"<{type(self).__name__} {self.name!r}>"

    class Trait(_Characteristic):
        @property
        def description(self) -> str:
            return self._md()

        def to_model(self) -> "model.Trait":
            return model.Trait(
                slug=self.slug, name=self.name,
                description=self.description, constraints=self.constraints,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "name": self.name,
                "description": _md_children(self._dd),
                "constraints": list(self._XP_CONSTRAINTS(self._dd)),
            }

    class Action(_Characteristic):
        @property
        def usage(self) -> str:
            return self._md()

        def to_model(self) -> "model.Action":
            return model.Action(
                slug=self.slug, name=self.name,
                usage=self.usage, constraints=self.constraints,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "name": self.name,
                "usage": _md_children(self._dd),
                "constraints": list(self._XP_CONSTRAINTS(self._dd)),
            }

    class Reaction(_Characteristic):
        @property
        def trigger(self) -> str:
            # TODO: HTML markup pending. Returns "" until
            # <span class="reaction-trigger"> and data-trigger-ids exist on
            # reaction dds.
            inline = domqf.first_text(
                self._dd, './/span[@class="reaction-trigger"]/text()'
            )
            if inline:
                return inline
            return domqf.resolve_reference(self._dd, "data-trigger-ids", join=" or ")

        @property
        def response(self) -> str:
            # TODO: HTML markup pending. Returns "" until
            # <span class="reaction-response"> exists on reaction dds.
            return domqf.first_text(
                self._dd, './/span[@class="reaction-response"]/text()'
            )

        def to_model(self) -> "model.Reaction":
            return model.Reaction(
                slug=self.slug, name=self.name,
                trigger=self.trigger, response=self.response,
                constraints=self.constraints,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "name": self.name,
                "trigger": self.trigger,
                "response": self.response,
                "constraints": list(self._XP_CONSTRAINTS(self._dd)),
            }

    class LegendaryAction(_Characteristic):
        @property
        def uses(self) -> int:
            # TODO: HTML markup pending. Returns 0 until the pool is
            # structurally marked (e.g.,
            # <span class="creature-legendary-action-uses">3</span>).
            return 0

        @property
        def situation_uses(self) -> dict:
            # TODO: HTML markup pending. Returns {} until situational pools
            # are structurally marked.
            return {}

        @property
        def usage(self) -> str:
            return self._md()

        def to_model(self) -> "model.LegendaryAction":
            return model.LegendaryAction(
                slug=self.slug, name=self.name,
                uses=self.uses, usage=self.usage,
                situation_uses=self.situation_uses, constraints=self.constraints,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "name": self.name,
                "uses": self.uses,
                "usage": _md_children(self._dd),
                "situation_uses": {k.value: v for k, v in self.situation_uses.items()},
                "constraints": list(self._XP_CONSTRAINTS(self._dd)),
            }

    class Creature:
        # Pre-compiled XPath expressions (built once, reused per instance)
        _XP_NAME_H3 = _letree.XPath('./h3[@class="creature-name"]/text()')
        _XP_NAME_H5 = _letree.XPath('./h5[@class="creature-name"]/text()')
        _XP_SIZE = _letree.XPath('.//span[@class="creature-size"]/text()')
        _XP_KIND = _letree.XPath('.//span[@class="creature-kind"]/text()')
        _XP_ALIGN = _letree.XPath('.//span[@class="creature-alignment"]/text()')
        _XP_AC = _letree.XPath('.//td[@class="creature-armor-class"]/text()')
        _XP_INIT = _letree.XPath('.//td[@class="creature-initiative"]/text()')
        _XP_HP = _letree.XPath('.//td[@class="creature-hit-points"]/text()')
        _XP_SPEED = _letree.XPath('.//td[@class="creature-speed"]/text()')
        _XP_SKILLS = _letree.XPath('.//td[@class="creature-skills"]/text()')
        _XP_SENSES = _letree.XPath('.//td[@class="creature-senses"]/text()')
        _XP_LANGS = _letree.XPath('.//td[@class="creature-languages"]/text()')
        _XP_CR = _letree.XPath('.//td[@class="creature-challenge-rating"]/text()')
        _XP_AB_STRENGTH = _letree.XPath('.//td[@class="creature-strength"]/text()')
        _XP_MOD_STRENGTH = _letree.XPath('.//td[@class="creature-strength-modifier"]/text()')
        _XP_SAV_STRENGTH = _letree.XPath('.//td[@class="creature-strength-save"]/text()')
        _XP_AB_DEXTERITY = _letree.XPath('.//td[@class="creature-dexterity"]/text()')
        _XP_MOD_DEXTERITY = _letree.XPath('.//td[@class="creature-dexterity-modifier"]/text()')
        _XP_SAV_DEXTERITY = _letree.XPath('.//td[@class="creature-dexterity-save"]/text()')
        _XP_AB_CONSTITUTION = _letree.XPath('.//td[@class="creature-constitution"]/text()')
        _XP_MOD_CONSTITUTION = _letree.XPath('.//td[@class="creature-constitution-modifier"]/text()')
        _XP_SAV_CONSTITUTION = _letree.XPath('.//td[@class="creature-constitution-save"]/text()')
        _XP_AB_INTELLIGENCE = _letree.XPath('.//td[@class="creature-intelligence"]/text()')
        _XP_MOD_INTELLIGENCE = _letree.XPath('.//td[@class="creature-intelligence-modifier"]/text()')
        _XP_SAV_INTELLIGENCE = _letree.XPath('.//td[@class="creature-intelligence-save"]/text()')
        _XP_AB_WISDOM = _letree.XPath('.//td[@class="creature-wisdom"]/text()')
        _XP_MOD_WISDOM = _letree.XPath('.//td[@class="creature-wisdom-modifier"]/text()')
        _XP_SAV_WISDOM = _letree.XPath('.//td[@class="creature-wisdom-save"]/text()')
        _XP_AB_CHARISMA = _letree.XPath('.//td[@class="creature-charisma"]/text()')
        _XP_MOD_CHARISMA = _letree.XPath('.//td[@class="creature-charisma-modifier"]/text()')
        _XP_SAV_CHARISMA = _letree.XPath('.//td[@class="creature-charisma-save"]/text()')

        def __init__(self, element: _Element):
            self._element = element

        @property
        def slug(self) -> str:
            return self._element.get("id", "").removeprefix("creature-")

        @property
        def name(self) -> str:
            r = self._XP_NAME_H3(self._element)
            if not r:
                r = self._XP_NAME_H5(self._element)
            return r[0] if r else ""

        @property
        def size(self) -> str:
            r = self._XP_SIZE(self._element); return r[0].strip() if r else ""

        @property
        def kind(self) -> str:
            r = self._XP_KIND(self._element); return r[0].strip() if r else ""

        @property
        def alignment(self) -> str:
            r = self._XP_ALIGN(self._element); return r[0].strip() if r else ""

        @property
        def armor_class(self) -> str:
            r = self._XP_AC(self._element); return r[0].strip() if r else ""

        @property
        def initiative(self) -> str:
            r = self._XP_INIT(self._element); return r[0].strip() if r else ""

        @property
        def hit_points(self) -> str:
            r = self._XP_HP(self._element); return r[0].strip() if r else ""

        @property
        def speed(self) -> str:
            r = self._XP_SPEED(self._element); return r[0].strip() if r else ""

        @property
        def skills(self) -> str:
            r = self._XP_SKILLS(self._element); return r[0].strip() if r else ""

        @property
        def senses(self) -> str:
            r = self._XP_SENSES(self._element); return r[0].strip() if r else ""

        @property
        def languages(self) -> str:
            r = self._XP_LANGS(self._element); return r[0].strip() if r else ""

        @property
        def challenge_rating(self) -> str:
            r = self._XP_CR(self._element); return r[0].strip() if r else ""

        @property
        def strength(self) -> int:
            r = self._XP_AB_STRENGTH(self._element)
            return int(r[0]) if r else 0

        @property
        def strength_modifier(self) -> int:
            r = self._XP_MOD_STRENGTH(self._element)
            return int(r[0]) if r else 0

        @property
        def strength_save(self) -> int:
            r = self._XP_SAV_STRENGTH(self._element)
            return int(r[0]) if r else 0

        @property
        def dexterity(self) -> int:
            r = self._XP_AB_DEXTERITY(self._element)
            return int(r[0]) if r else 0

        @property
        def dexterity_modifier(self) -> int:
            r = self._XP_MOD_DEXTERITY(self._element)
            return int(r[0]) if r else 0

        @property
        def dexterity_save(self) -> int:
            r = self._XP_SAV_DEXTERITY(self._element)
            return int(r[0]) if r else 0

        @property
        def constitution(self) -> int:
            r = self._XP_AB_CONSTITUTION(self._element)
            return int(r[0]) if r else 0

        @property
        def constitution_modifier(self) -> int:
            r = self._XP_MOD_CONSTITUTION(self._element)
            return int(r[0]) if r else 0

        @property
        def constitution_save(self) -> int:
            r = self._XP_SAV_CONSTITUTION(self._element)
            return int(r[0]) if r else 0

        @property
        def intelligence(self) -> int:
            r = self._XP_AB_INTELLIGENCE(self._element)
            return int(r[0]) if r else 0

        @property
        def intelligence_modifier(self) -> int:
            r = self._XP_MOD_INTELLIGENCE(self._element)
            return int(r[0]) if r else 0

        @property
        def intelligence_save(self) -> int:
            r = self._XP_SAV_INTELLIGENCE(self._element)
            return int(r[0]) if r else 0

        @property
        def wisdom(self) -> int:
            r = self._XP_AB_WISDOM(self._element)
            return int(r[0]) if r else 0

        @property
        def wisdom_modifier(self) -> int:
            r = self._XP_MOD_WISDOM(self._element)
            return int(r[0]) if r else 0

        @property
        def wisdom_save(self) -> int:
            r = self._XP_SAV_WISDOM(self._element)
            return int(r[0]) if r else 0

        @property
        def charisma(self) -> int:
            r = self._XP_AB_CHARISMA(self._element)
            return int(r[0]) if r else 0

        @property
        def charisma_modifier(self) -> int:
            r = self._XP_MOD_CHARISMA(self._element)
            return int(r[0]) if r else 0

        @property
        def charisma_save(self) -> int:
            r = self._XP_SAV_CHARISMA(self._element)
            return int(r[0]) if r else 0

        @property
        def traits(self) -> List["query.Trait"]:
            return self._named_items("creature-traits", query.Trait)

        @property
        def actions(self) -> List["query.Action"]:
            return self._named_items("creature-actions", query.Action)

        @property
        def bonus_actions(self) -> List["query.Action"]:
            return self._named_items("creature-bonus-actions", query.Action)

        @property
        def reactions(self) -> List["query.Reaction"]:
            return self._named_items("creature-reactions", query.Reaction)

        @property
        def legendary_actions(self) -> List["query.LegendaryAction"]:
            return self._named_items("creature-legendary-actions", query.LegendaryAction)

        _XP_DTS = _letree.XPath('./dl/dt')
        _XP_DDS = _letree.XPath('./dl/dd')

        def _named_items(self, section_class: str, ctor):
            section = self._element.xpath(f'./section[@class="{section_class}"]')
            if not section:
                return []
            dts = self._XP_DTS(section[0])
            dds = self._XP_DDS(section[0])
            return [ctor(dt, dd) for dt, dd in zip(dts, dds)]

        def to_model(self) -> "model.Creature":
            return model.Creature(
                slug=self.slug,
                name=self.name,
                size=self.size,
                kind=self.kind,
                alignment=self.alignment,
                armor_class=self.armor_class,
                initiative=self.initiative,
                hit_points=self.hit_points,
                speed=self.speed,
                strength=self.strength,
                strength_modifier=self.strength_modifier,
                strength_save=self.strength_save,
                dexterity=self.dexterity,
                dexterity_modifier=self.dexterity_modifier,
                dexterity_save=self.dexterity_save,
                constitution=self.constitution,
                constitution_modifier=self.constitution_modifier,
                constitution_save=self.constitution_save,
                intelligence=self.intelligence,
                intelligence_modifier=self.intelligence_modifier,
                intelligence_save=self.intelligence_save,
                wisdom=self.wisdom,
                wisdom_modifier=self.wisdom_modifier,
                wisdom_save=self.wisdom_save,
                charisma=self.charisma,
                charisma_modifier=self.charisma_modifier,
                charisma_save=self.charisma_save,
                skills=self.skills,
                senses=self.senses,
                languages=self.languages,
                challenge_rating=self.challenge_rating,
                traits=[t.to_model() for t in self.traits],
                actions=[a.to_model() for a in self.actions],
                bonus_actions=[a.to_model() for a in self.bonus_actions],
                reactions=[r.to_model() for r in self.reactions],
                legendary_actions=[la.to_model() for la in self.legendary_actions],
            )

        def to_dict(self) -> dict:
            """Direct dict materialization (skips model.Creature dataclass)."""
            return {
                "slug": self.slug,
                "name": self.name,
                "size": self.size,
                "kind": self.kind,
                "alignment": self.alignment,
                "armor_class": self.armor_class,
                "initiative": self.initiative,
                "hit_points": self.hit_points,
                "speed": self.speed,
                "strength": self.strength,
                "strength_modifier": self.strength_modifier,
                "strength_save": self.strength_save,
                "dexterity": self.dexterity,
                "dexterity_modifier": self.dexterity_modifier,
                "dexterity_save": self.dexterity_save,
                "constitution": self.constitution,
                "constitution_modifier": self.constitution_modifier,
                "constitution_save": self.constitution_save,
                "intelligence": self.intelligence,
                "intelligence_modifier": self.intelligence_modifier,
                "intelligence_save": self.intelligence_save,
                "wisdom": self.wisdom,
                "wisdom_modifier": self.wisdom_modifier,
                "wisdom_save": self.wisdom_save,
                "charisma": self.charisma,
                "charisma_modifier": self.charisma_modifier,
                "charisma_save": self.charisma_save,
                "skills": self.skills,
                "senses": self.senses,
                "languages": self.languages,
                "challenge_rating": self.challenge_rating,
                "traits": [t.to_dict() for t in self.traits],
                "actions": [a.to_dict() for a in self.actions],
                "bonus_actions": [a.to_dict() for a in self.bonus_actions],
                "reactions": [r.to_dict() for r in self.reactions],
                "legendary_actions": [la.to_dict() for la in self.legendary_actions],
            }

        def __repr__(self) -> str:
            return f"<query.Creature {self.slug!r}: {self.size} {self.kind}>"


    class SpellEffect(_Characteristic):
        """A named effect on a spell (spell-effect dl/dt/dd)."""

        @property
        def description(self) -> str:
            return self._md()

        def to_model(self) -> "model.SpellEffect":
            return model.SpellEffect(
                slug=self.slug, name=self.name,
                description=self.description, constraints=self.constraints,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "name": self.name,
                "description": _md_children(self._dd),
                "constraints": list(self._XP_CONSTRAINTS(self._dd)),
            }


    class SpecialRules:
        """An embedded rules subsection (Bag of Tricks color variants, Wand of Wonder
        Effects table, Control Weather Precipitation table, etc.)."""

        _XP_HEADING = _letree.XPath('./h5/text() | ./h6/text()')

        def __init__(self, element: _Element):
            self._element = element

        @property
        def slug(self) -> str:
            full_id = self._element.get("id", "")
            parts = full_id.split("-special-")
            return parts[1] if len(parts) == 2 else full_id

        @property
        def heading(self) -> str:
            r = self._XP_HEADING(self._element)
            return r[0] if r else ""

        @property
        def content(self) -> str:
            return _md_children(self._element)

        def to_model(self) -> "model.SpecialRules":
            return model.SpecialRules(
                slug=self.slug, heading=self.heading, content=self.content,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "heading": self.heading,
                "content": _md_children(self._element),
            }


    class Spell:
        """A spell entry in SRDOM."""

        _XP_NAME = _letree.XPath('./h4[@class="spell-name"]/text()')
        _XP_LEVEL = _letree.XPath(
            './p[@class="spell-general"]/span[@class="spell-level"]/text()'
        )
        _XP_UPGRADE = _letree.XPath(
            './p[@class="spell-general"]/span[@class="spell-upgrade"]/text()'
        )
        _XP_SCHOOL = _letree.XPath(
            './p[@class="spell-general"]/span[@class="spell-school"]/text()'
        )
        _XP_CLASSES = _letree.XPath(
            './p[@class="spell-general"]/span[@class="spell-classes"]/text()'
        )
        _XP_CASTING_TIME = _letree.XPath(
            './table[@class="spell-cast"]/tr/td[@class="spell-casting-time"]/text()'
        )
        _XP_RANGE = _letree.XPath(
            './table[@class="spell-cast"]/tr/td[@class="spell-range"]/text()'
        )
        _XP_COMPONENTS = _letree.XPath(
            './table[@class="spell-cast"]/tr/td[@class="spell-components"]/text()'
        )
        _XP_DURATION = _letree.XPath(
            './table[@class="spell-cast"]/tr/td[@class="spell-duration"]/text()'
        )
        _XP_DESCRIPTION = _letree.XPath('./div[@class="spell-description"]')
        _XP_EFFECTS_DL = _letree.XPath('./dl[@class="spell-effects"]')
        _XP_EFFECTS_DTS = _letree.XPath('./dt')
        _XP_EFFECTS_DDS = _letree.XPath('./dd')
        _XP_SPECIALS = _letree.XPath(
            './div[@class="spell-specials"]/section[@class="spell-special"]'
        )
        _XP_EMBEDDED_CREATURE = _letree.XPath('./section[@class="creature"]')

        def __init__(self, element: _Element):
            self._element = element

        @property
        def slug(self) -> str:
            return self._element.get("id", "").removeprefix("spell-")

        @property
        def name(self) -> str:
            r = self._XP_NAME(self._element)
            return r[0] if r else ""

        @property
        def level(self) -> int:
            """Spell level. 0 for cantrips, 1-9 for leveled spells."""
            r = self._XP_LEVEL(self._element)
            return int(r[0]) if r else 0

        @property
        def upgrade(self) -> Optional[str]:
            """At-higher-levels text, or 'Cantrip' marker for cantrips, or None."""
            r = self._XP_UPGRADE(self._element)
            return r[0] if r else None

        @property
        def school(self) -> str:
            r = self._XP_SCHOOL(self._element)
            return r[0] if r else ""

        @property
        def classes(self) -> List[str]:
            r = self._XP_CLASSES(self._element)
            if not r:
                return []
            return [c.strip() for c in r[0].split(",")]

        @property
        def casting_time(self) -> str:
            r = self._XP_CASTING_TIME(self._element)
            return r[0] if r else ""

        @property
        def range(self) -> str:
            r = self._XP_RANGE(self._element)
            return r[0] if r else ""

        @property
        def components(self) -> "model.SpellComponents":
            r = self._XP_COMPONENTS(self._element)
            raw = r[0] if r else ""
            return _parse_spell_components(raw)

        @property
        def duration(self) -> str:
            r = self._XP_DURATION(self._element)
            return r[0] if r else ""

        @property
        def description(self) -> str:
            descs = self._XP_DESCRIPTION(self._element)
            return _md_children(descs[0]) if descs else ""

        @property
        def effects(self) -> "List[query.SpellEffect]":
            dls = self._XP_EFFECTS_DL(self._element)
            if not dls:
                return []
            dl = dls[0]
            dts = self._XP_EFFECTS_DTS(dl)
            dds = self._XP_EFFECTS_DDS(dl)
            return [query.SpellEffect(dt, dd) for dt, dd in zip(dts, dds)]

        @property
        def special_rules(self) -> "List[query.SpecialRules]":
            return [query.SpecialRules(sec) for sec in self._XP_SPECIALS(self._element)]

        @property
        def creature(self) -> "Optional[query.Creature]":
            r = self._XP_EMBEDDED_CREATURE(self._element)
            return query.Creature(r[0]) if r else None

        def to_model(self) -> "model.Spell":
            embedded = self.creature
            return model.Spell(
                slug=self.slug, name=self.name, level=self.level,
                upgrade=self.upgrade, school=self.school, classes=self.classes,
                casting_time=self.casting_time, range=self.range,
                components=self.components, duration=self.duration,
                description=self.description,
                effects=[e.to_model() for e in self.effects],
                special_rules=[s.to_model() for s in self.special_rules],
                creature=embedded.to_model() if embedded else None,
            )

        def to_dict(self) -> dict:
            embedded = self.creature
            comps = self.components
            return {
                "slug": self.slug,
                "name": self.name,
                "level": self.level,
                "upgrade": self.upgrade,
                "school": self.school,
                "classes": self.classes,
                "casting_time": self.casting_time,
                "range": self.range,
                "components": {
                    "verbal": comps.verbal,
                    "somatic": comps.somatic,
                    "material": comps.material,
                },
                "duration": self.duration,
                "description": self.description,
                "effects": [e.to_dict() for e in self.effects],
                "special_rules": [s.to_dict() for s in self.special_rules],
                "creature": embedded.to_dict() if embedded else None,
            }

        def __repr__(self) -> str:
            if self.level == 0:
                return f"<query.Spell {self.slug!r}: {self.school} Cantrip>"
            return f"<query.Spell {self.slug!r}: Level {self.level} {self.school}>"


    class MagicItem:
        """A magic item entry in SRDOM."""

        _XP_TITLE = _letree.XPath('./h4[@class="magic-item-title"]')
        _XP_NAME = _letree.XPath(
            './h4[@class="magic-item-title"]/span[@class="magic-item-name"]/text()'
        )
        _XP_VARIANTS = _letree.XPath(
            './h4[@class="magic-item-title"]/span[@class="magic-item-variant"]/text()'
        )
        _XP_CATEGORY = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-category"]/text()'
        )
        _XP_CATEGORY_DESC = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-category-description"]/text()'
        )
        _XP_RARITIES = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-rarities"]'
            '/span[@class="magic-item-rarity"]/text()'
        )
        _XP_ATTUNEMENT = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-attunement"]/text()'
        )
        _XP_DESCRIPTION = _letree.XPath('./div[@class="magic-item-description"]')
        _XP_SPECIALS = _letree.XPath(
            './div[@class="magic-item-specials"]/section[@class="magic-item-special"]'
        )
        _XP_EMBEDDED_CREATURE = _letree.XPath(
            './div[@class="magic-item-description"]//section[@class="creature"]'
        )

        def __init__(self, element: _Element):
            self._element = element

        @property
        def slug(self) -> str:
            return self._element.get("id", "").removeprefix("magic-item-")

        @property
        def title(self) -> str:
            h4 = self._XP_TITLE(self._element)
            return h4[0].text_content().strip() if h4 else ""

        @property
        def name(self) -> str:
            r = self._XP_NAME(self._element)
            return r[0] if r else ""

        @property
        def variants(self) -> List[str]:
            return list(self._XP_VARIANTS(self._element))

        @property
        def category(self) -> str:
            r = self._XP_CATEGORY(self._element)
            return r[0] if r else ""

        @property
        def category_description(self) -> Optional[str]:
            r = self._XP_CATEGORY_DESC(self._element)
            return r[0] if r else None

        @property
        def rarities(self) -> List[str]:
            return list(self._XP_RARITIES(self._element))

        @property
        def rarity_tiers(self) -> List[dict]:
            rarities = self.rarities
            variants = self.variants
            if not variants:
                return [{"rarity": r, "variant": None} for r in rarities]
            return [{"rarity": r, "variant": v} for r, v in zip(rarities, variants)]

        @property
        def attunement(self) -> Optional[str]:
            r = self._XP_ATTUNEMENT(self._element)
            return r[0] if r else None

        @property
        def description(self) -> str:
            descs = self._XP_DESCRIPTION(self._element)
            return _md_children(descs[0]) if descs else ""

        @property
        def special_rules(self) -> "List[query.SpecialRules]":
            return [query.SpecialRules(sec) for sec in self._XP_SPECIALS(self._element)]

        @property
        def creature(self) -> "Optional[query.Creature]":
            r = self._XP_EMBEDDED_CREATURE(self._element)
            return query.Creature(r[0]) if r else None

        def to_model(self) -> "model.MagicItem":
            embedded = self.creature
            tiers = [
                model.MagicItemRarityTier(rarity=t["rarity"], variant=t["variant"])
                for t in self.rarity_tiers
            ]
            return model.MagicItem(
                slug=self.slug, title=self.title, name=self.name,
                variants=self.variants, category=self.category,
                category_description=self.category_description,
                rarities=self.rarities, rarity_tiers=tiers,
                attunement=self.attunement, description=self.description,
                special_rules=[s.to_model() for s in self.special_rules],
                creature=embedded.to_model() if embedded else None,
            )

        def to_dict(self) -> dict:
            embedded = self.creature
            return {
                "slug": self.slug,
                "title": self.title,
                "name": self.name,
                "variants": self.variants,
                "category": self.category,
                "category_description": self.category_description,
                "rarities": self.rarities,
                "rarity_tiers": self.rarity_tiers,
                "attunement": self.attunement,
                "description": self.description,
                "special_rules": [s.to_dict() for s in self.special_rules],
                "creature": embedded.to_dict() if embedded else None,
            }

        def __repr__(self) -> str:
            cd = f" ({self.category_description})" if self.category_description else ""
            rarities = ", ".join(self.rarities)
            return f"<query.MagicItem {self.slug!r}: {self.category}{cd}, {rarities}>"


def _parse_spell_components(raw: str) -> "model.SpellComponents":
    """Parse a 'Verbal, Somatic, Material (...)' string into SpellComponents."""
    verbal = "Verbal" in raw
    somatic = "Somatic" in raw
    material = None
    if "Material" in raw:
        m = re.search(r"Material\s*\(([^)]+)\)", raw)
        material = m.group(1) if m else ""
    return model.SpellComponents(verbal=verbal, somatic=somatic, material=material)




# ============================================================================
# Document and Collection (creature-focused for prototype)
# ============================================================================


def load(source: Optional[str] = None) -> "Document":
    """Load an SRDOM document. Returns a Document with .creatures."""
    path, version = _resolve_source(source)
    with open(path, "rb") as f:
        tree = _lhtml.fromstring(f.read())
    return Document(tree, version=version, path=path)


class Document:
    """A parsed SRDOM document.

    Provides indexed access to the document's spells, creatures, and magic items.
    """

    def __init__(self, tree, version=None, path=None):
        self._tree = tree
        self._version = version or self._read_version_from_tree()
        self._path = path
        self._spells = SpellCollection(tree)
        self._creatures = CreatureCollection(tree)
        self._magic_items = MagicItemCollection(tree)

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
    def magic_items(self) -> "MagicItemCollection":
        return self._magic_items

    @property
    def version(self) -> Optional[str]:
        return self._version

    def __repr__(self) -> str:
        return (
            f"<Document version={self._version!r} "
            f"spells={len(self._spells)} creatures={len(self._creatures)} "
            f"magic_items={len(self._magic_items)}>"
        )


class _BaseCollection:
    """Shared indexable/iterable collection over a section class."""

    _entity_class = None
    _id_prefix = None
    _section_class = None

    def __init__(self, tree: _Element):
        self._tree = tree

    def __getitem__(self, slug: str):
        result = self._tree.xpath(f'id("{self._id_prefix}{slug}")')
        if not result:
            raise KeyError(slug)
        return self._entity_class(result[0])

    def __iter__(self):
        for el in self._tree.xpath(f'//section[@class="{self._section_class}"]'):
            yield self._entity_class(el)

    def __len__(self) -> int:
        return len(self._tree.xpath(f'//section[@class="{self._section_class}"]'))

    def __contains__(self, slug: str) -> bool:
        return bool(self._tree.xpath(f'id("{self._id_prefix}{slug}")'))


class SpellCollection(_BaseCollection):
    """All spells. Indexable by slug, iterable. Returns query.Spell."""
    _section_class = "spell"
    _id_prefix = "spell-"


class CreatureCollection(_BaseCollection):
    """All creatures. Indexable by slug, iterable. Returns query.Creature."""
    _section_class = "creature"
    _id_prefix = "creature-"


class MagicItemCollection(_BaseCollection):
    """All magic items. Indexable by slug, iterable. Returns query.MagicItem."""
    _section_class = "magic-item"
    _id_prefix = "magic-item-"


# Resolve forward references for collection entity types
SpellCollection._entity_class = query.Spell
CreatureCollection._entity_class = query.Creature
MagicItemCollection._entity_class = query.MagicItem




# ============================================================================
# CLI - JSON output via query.X.to_dict()
# ============================================================================


def _encode(obj):
    """Recursively encode model.* dataclasses for JSON."""
    if dataclasses.is_dataclass(obj):
        return {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {_encode(k): _encode(v) for k, v in obj.items()}
    return obj


def _read_or_build_json_cache(version: str, entity_type: str, doc: "Document") -> list:
    """Read a JSON cache file if present; otherwise build it from doc and write."""
    cache_path = _json_cache_path(version, entity_type)
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if entity_type == "spells":
        data = [s.to_dict() for s in doc.spells]
    elif entity_type == "creatures":
        data = [c.to_dict() for c in doc.creatures]
    elif entity_type == "magic_items":
        data = [m.to_dict() for m in doc.magic_items]
    else:
        raise ValueError(f"Unknown entity_type: {entity_type!r}")
    _ensure_cache_dir()
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def _cmd_collection(args, entity_type: str) -> int:
    doc = load(source=args.source)
    data = _read_or_build_json_cache(doc.version, entity_type, doc)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_entity(args, entity_type: str) -> int:
    doc = load(source=args.source)
    if entity_type == "spells":
        collection, singular = doc.spells, "spell"
    elif entity_type == "creatures":
        collection, singular = doc.creatures, "creature"
    elif entity_type == "magic_items":
        collection, singular = doc.magic_items, "magic-item"
    else:
        raise ValueError(f"Unknown entity_type: {entity_type!r}")
    if args.slug not in collection:
        sys.stderr.write(f"No {singular} with slug: {args.slug!r}\n")
        return 1
    entity = collection[args.slug]
    json.dump(entity.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_test(args) -> int:
    return tests.run_all(source=args.source)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srdom",
        description=(
            "Query SRDOM (D&D SRD 5.2.1 as structured HTML). "
            "Emits JSON to stdout for pipeline use."
        ),
    )
    p.add_argument(
        "--source", default=None,
        help=(
            "Source resolution: 'refresh' (force redownload), a filepath, "
            f"or a URL. Default: cache, or fetch from {DEFAULT_URL} if absent/stale."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("spells", help="Emit all spells as JSON.")
    sub.add_parser("creatures", help="Emit all creatures as JSON.")
    sub.add_parser("magic-items", help="Emit all magic items as JSON.")
    sp = sub.add_parser("spell", help="Emit one spell as JSON.")
    sp.add_argument("slug")
    sc = sub.add_parser("creature", help="Emit one creature as JSON.")
    sc.add_argument("slug")
    sm = sub.add_parser("magic-item", help="Emit one magic item as JSON.")
    sm.add_argument("slug")
    sub.add_parser("test", help="Run a self-test against the resolved source.")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "spells":      return _cmd_collection(args, "spells")
    if args.command == "creatures":   return _cmd_collection(args, "creatures")
    if args.command == "magic-items": return _cmd_collection(args, "magic_items")
    if args.command == "spell":       return _cmd_entity(args, "spells")
    if args.command == "creature":    return _cmd_entity(args, "creatures")
    if args.command == "magic-item":  return _cmd_entity(args, "magic_items")
    if args.command == "test":        return _cmd_test(args)
    return 2


# ============================================================================
# Tests
# ============================================================================


class tests:
    """Prototype self-tests. Each test takes a loaded Document."""

    @staticmethod
    def run_all(source: Optional[str] = None) -> int:
        print(f"Loading source (source={source!r})...")
        doc = load(source=source)
        print(f"  Version: {doc.version}")
        print(f"  Spells:      {len(doc.spells)}")
        print(f"  Creatures:   {len(doc.creatures)}")
        print(f"  Magic items: {len(doc.magic_items)}\n")

        names = sorted(n for n in dir(tests) if n.startswith("test_"))
        failed = 0
        for name in names:
            try:
                getattr(tests, name)(doc)
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}")
                print(f"        {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
        total = len(names)
        print(f"\n{total - failed}/{total} tests passed")
        return 0 if failed == 0 else 1

    # ---- Normal behavior ----

    @staticmethod
    def test_creature_count(doc):
        assert len(doc.creatures) == 336, \
            f"expected 336 creatures, got {len(doc.creatures)}"

    @staticmethod
    def test_aboleth_basic_fields(doc):
        c = doc.creatures["aboleth"]
        assert c.name == "Aboleth", f"name {c.name!r}"
        assert c.size == "Large", f"size {c.size!r}"
        assert c.kind == "Aberration", f"kind {c.kind!r}"
        assert c.strength == 21, f"strength {c.strength!r}"
        assert c.charisma == 18, f"charisma {c.charisma!r}"
        assert c.armor_class == "17", f"armor_class {c.armor_class!r}"
        assert c.challenge_rating.startswith("10"), f"cr {c.challenge_rating!r}"

    @staticmethod
    def test_aboleth_ability_types(doc):
        c = doc.creatures["aboleth"]
        # Ability scores are u32, modifiers and saves are i32
        assert isinstance(c.strength, int)
        assert isinstance(c.dexterity_modifier, int)
        assert isinstance(c.constitution_save, int)
        # Modifier and save can be negative
        assert c.dexterity_modifier == -1, f"dex_mod {c.dexterity_modifier!r}"

    @staticmethod
    def test_aboleth_traits(doc):
        c = doc.creatures["aboleth"]
        traits = list(c.traits)
        assert len(traits) == 5, f"expected 5 traits, got {len(traits)}"
        names = [t.name for t in traits]
        assert "Amphibious" in names
        assert "Legendary Resistance" in names

    @staticmethod
    def test_constraint_is_vec(doc):
        # Legendary Resistance has a 3/Day constraint on most creatures
        c = doc.creatures["aboleth"]
        lr = next(t for t in c.traits if t.name == "Legendary Resistance")
        assert isinstance(lr.constraints, list)
        assert len(lr.constraints) >= 1, f"expected >=1 constraint, got {lr.constraints!r}"
        assert "3/Day" in lr.constraints[0] or "Day" in lr.constraints[0]

    @staticmethod
    def test_aboleth_actions(doc):
        c = doc.creatures["aboleth"]
        actions = list(c.actions)
        assert len(actions) >= 2, f"expected >=2 actions, got {len(actions)}"
        names = [a.name for a in actions]
        assert "Multiattack" in names

    @staticmethod
    def test_aboleth_legendary_actions(doc):
        c = doc.creatures["aboleth"]
        legendary = list(c.legendary_actions)
        assert len(legendary) == 2, f"expected 2 legendary actions, got {len(legendary)}"
        names = [la.name for la in legendary]
        assert "Lash" in names
        assert "Psychic Drain" in names

    @staticmethod
    def test_to_model_creature(doc):
        c = doc.creatures["aboleth"]
        m = c.to_model()
        assert isinstance(m, model.Creature)
        assert m.slug == "aboleth"
        assert m.name == "Aboleth"
        assert m.strength == 21
        assert len(m.traits) == 5
        assert len(m.legendary_actions) == 2
        # Each trait should be a model.Trait dataclass
        assert isinstance(m.traits[0], model.Trait)
        # Model.Trait.description is plain str (Content lives on the query side)
        assert isinstance(m.traits[0].description, str)

    @staticmethod
    def test_chain_devil_unnerving_gaze_intact(doc):
        # Regression: previously had a parsing artifact split. Should be one reaction.
        c = doc.creatures["chain-devil"]
        reactions = list(c.reactions)
        assert len(reactions) == 1, \
            f"expected 1 reaction (Unnerving Gaze), got {len(reactions)}"
        assert reactions[0].name == "Unnerving Gaze"

    # ---- TODO placeholder behavior ----
    # These confirm that fields awaiting HTML markup return their expected
    # placeholder values. When the markup is added, these tests SHOULD FAIL,
    # signaling that they need to be updated.

    @staticmethod
    def test_TODO_reaction_trigger_returns_empty(doc):
        # The HTML doesn't yet have <span class="reaction-trigger"> nor
        # data-trigger-ids attributes. Trigger should be "" for all reactions.
        c = doc.creatures["chain-devil"]
        r = list(c.reactions)[0]
        assert r.trigger == "", (
            f"expected '' (markup pending), got {r.trigger!r}. "
            f"If <span class=\"reaction-trigger\"> was added to the HTML, "
            f"update this test."
        )

    @staticmethod
    def test_TODO_reaction_response_returns_empty(doc):
        c = doc.creatures["chain-devil"]
        r = list(c.reactions)[0]
        assert r.response == "", (
            f"expected '' (markup pending), got {r.response!r}. "
            f"If <span class=\"reaction-response\"> was added, update this test."
        )

    @staticmethod
    def test_TODO_legendary_action_uses_zero(doc):
        c = doc.creatures["aboleth"]
        la = list(c.legendary_actions)[0]
        assert la.uses == 0, (
            f"expected 0 (markup pending), got {la.uses}. "
            f"If uses is now structurally marked, update this test."
        )

    @staticmethod
    def test_TODO_legendary_action_situation_uses_empty(doc):
        c = doc.creatures["aboleth"]
        la = list(c.legendary_actions)[0]
        assert la.situation_uses == {}, (
            f"expected {{}} (markup pending), got {la.situation_uses!r}. "
            f"If situational pools are now structurally marked, update this test."
        )

    # ---- Spell tests ----

    @staticmethod
    def test_spell_count(doc):
        assert len(doc.spells) >= 300, f"expected 300+ spells, got {len(doc.spells)}"

    @staticmethod
    def test_fireball_basic_fields(doc):
        s = doc.spells["fireball"]
        assert s.name == "Fireball"
        assert s.level == 3
        assert s.school == "Evocation"
        assert "Sorcerer" in s.classes and "Wizard" in s.classes
        assert s.casting_time == "Action"
        comps = s.components
        assert comps.verbal is True and comps.somatic is True
        assert comps.material and "bat guano" in comps.material

    @staticmethod
    def test_cantrip_is_level_zero(doc):
        s = doc.spells["acid-splash"]
        assert s.level == 0, f"cantrip should be level 0, got {s.level!r}"
        assert s.school == "Evocation"

    @staticmethod
    def test_spell_to_model_roundtrip(doc):
        s = doc.spells["fireball"]
        m = s.to_model()
        assert isinstance(m, model.Spell)
        assert m.level == 3
        assert isinstance(m.components, model.SpellComponents)

    # ---- Magic item tests ----

    @staticmethod
    def test_magic_item_count(doc):
        assert len(doc.magic_items) >= 200, (
            f"expected 200+ magic items, got {len(doc.magic_items)}"
        )

    @staticmethod
    def test_holy_avenger_basic_fields(doc):
        m = doc.magic_items["holy-avenger"]
        assert m.name == "Holy Avenger"
        assert m.category == "Weapon"
        assert "Legendary" in m.rarities
        assert m.attunement is not None

    @staticmethod
    def test_weapon_has_variants(doc):
        m = doc.magic_items["weapon"]
        assert m.variants == ["+1", "+2", "+3"]
        assert len(m.rarity_tiers) == 3
        assert m.rarity_tiers[0]["variant"] == "+1"

    @staticmethod
    def test_figurine_has_embedded_creature(doc):
        m = doc.magic_items["figurine-of-wondrous-power"]
        assert m.creature is not None, (
            "figurine-of-wondrous-power should have an embedded creature"
        )

    @staticmethod
    def test_magic_item_to_model_roundtrip(doc):
        m = doc.magic_items["adamantine-armor"]
        mm = m.to_model()
        assert isinstance(mm, model.MagicItem)
        assert mm.category == "Armor"


if __name__ == "__main__":
    sys.exit(main())
