"""
srdom — query SRDOM (SRD 5.2.1 as structured HTML, 5E compatible).

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
from typing import Any, Iterator, List, Optional, Tuple, Union

try:
    from lxml import etree as _letree
    from lxml import html as _lhtml
    from lxml.etree import _Element
except ImportError as e:
    raise ImportError("srdom requires lxml") from e


__version__ = "0.7.2"
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


_JSON_CACHE_ENTITIES = ("spells", "creatures", "magic_items")


def _invalidate_json_caches(version: str) -> None:
    """Delete any JSON cache files for the given version (so stale derivations
    don't outlive a fresh HTML mirror)."""
    for entity in _JSON_CACHE_ENTITIES:
        p = _json_cache_path(version, entity)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def _mirror_to_cache(data: bytes, version: str) -> Path:
    """Write HTML bytes to the canonical cache slot, update the VERSION marker,
    and invalidate any matching JSON caches. Returns the cache file path."""
    _ensure_cache_dir()
    cache_path = _html_cache_path(version)
    cache_path.write_bytes(data)
    _write_cache_version(version)
    _invalidate_json_caches(version)
    return cache_path


def _discover_local_srdom() -> Optional[Path]:
    """Look for an srdom.html file colocated with the user's work:
    1. current working directory
    2. directory containing this script
    Returns the first existing path, or None."""
    candidates: List[Path] = [Path.cwd() / "srdom.html"]
    try:
        candidates.append(Path(__file__).resolve().parent / "srdom.html")
    except NameError:
        pass
    seen = set()
    for p in candidates:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        if rp.is_file():
            return rp
    return None


def _resolve_source(source: Optional[str]) -> Tuple[Path, str]:
    """Resolve the source argument to a (file_path, version) tuple.

    Resolution order:
    1. Explicit --source <X>: file, URL, or "refresh"
    2. Auto-discovered ./srdom.html (cwd)
    3. Auto-discovered <script-dir>/srdom.html
    4. Fresh cache
    5. Fetch from DEFAULT_URL

    Any path that produces HTML bytes outside the cache (steps 1-3, 5) mirrors
    those bytes into the cache and invalidates matching JSON caches. The cache
    always reflects the last source we resolved against.
    """
    # Step 1: explicit
    if source == "refresh":
        return _fetch_and_cache(DEFAULT_URL)
    if source and _is_url(source):
        return _fetch_and_cache(source)
    if source:  # explicit local filepath
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"No such file: {source}")
        data = p.read_bytes()
        version = _extract_version(data)
        cache_path = _mirror_to_cache(data, version)
        return (cache_path, version)

    # Steps 2-3: auto-discovery
    local = _discover_local_srdom()
    if local is not None:
        data = local.read_bytes()
        version = _extract_version(data)
        cache_path = _mirror_to_cache(data, version)
        return (cache_path, version)

    # Step 4: cache fallback
    cached_version = _read_cache_version()
    if cached_version:
        cache_path = _html_cache_path(cached_version)
        if _is_cache_fresh(cache_path):
            return (cache_path, cached_version)

    # Step 5: URL fetch
    return _fetch_and_cache(DEFAULT_URL)


def _fetch_and_cache(url: str) -> Tuple[Path, str]:
    """Fetch a URL, mirror its bytes into the cache, update VERSION,
    and invalidate matching JSON caches."""
    data = _fetch_url(url)
    version = _extract_version(data)
    cache_path = _mirror_to_cache(data, version)
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


def _md_content(element: _Element) -> str:
    """Render an element's full content as Markdown — including the element's
    leading .text (before the first child) and all block children. Use for
    elements like <dd> that may carry text-only content or mixed text+children."""
    parts = []
    if element.text and element.text.strip():
        parts.append(element.text.strip())
    for child in element:
        rendered = _md_block(child)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts).strip()


def _slugify(text: str) -> str:
    """Convert text to a strict-minimal slug: apostrophes stripped (both straight
    and curly), lowercase, non-alphanumeric → '-', collapse runs of '-', trim
    leading/trailing '-'. Apostrophes are stripped (not replaced) so possessive
    forms like 'Giant's' slug to 'giants', not 'giant-s'."""
    s = (text or "").replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def _slug_build(*parts: str) -> str:
    """Build a compound slug from parts: slugify each, join with '-',
    collapse runs of '-', trim."""
    pieces = [_slugify(p) for p in parts if p]
    joined = "-".join(pieces)
    return re.sub(r"-{2,}", "-", joined).strip("-")


_RARITY_STR_MAP = {
    "common": "common",
    "uncommon": "uncommon",
    "rare": "rare",
    "very rare": "very_rare",
    "very_rare": "very_rare",
    "legendary": "legendary",
    "artifact": "artifact",
}


def _normalize_rarity(text: Optional[str]) -> Optional[str]:
    """Normalize HTML rarity text (e.g. 'Very Rare') to enum value ('very_rare').
    Returns None for unrecognized strings (including 'Rarity Varies')."""
    if not text:
        return None
    key = text.strip().lower()
    return _RARITY_STR_MAP.get(key)



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
        initiative_modifier: Optional[int]
        passive_perception: int
        passive_initiative: Optional[int]
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

    class Rarity(str, Enum):
        """SRD 5.2.1 magic item rarity tiers in canonical order."""
        COMMON = "common"
        UNCOMMON = "uncommon"
        RARE = "rare"
        VERY_RARE = "very_rare"
        LEGENDARY = "legendary"
        ARTIFACT = "artifact"

    # ----- DOMMF: union attunement_requirement -----
    # Each variant is a frozen dataclass for value semantics (==, hashable);
    # collectively they form the AttunementRequirement sum type.

    @dataclass(frozen=True)
    class AttunementAny:
        """attunement_requirement::any -- bare 'Requires Attunement' (no qualifier)."""

    class SrdClass(str, Enum):
        """SRD 5.2.1 player classes (hardcoded enum, per srdom.dommf)."""
        BARBARIAN = "barbarian"
        BARD = "bard"
        CLERIC = "cleric"
        DRUID = "druid"
        FIGHTER = "fighter"
        MONK = "monk"
        PALADIN = "paladin"
        RANGER = "ranger"
        ROGUE = "rogue"
        SORCERER = "sorcerer"
        WARLOCK = "warlock"
        WIZARD = "wizard"

    class SrdLineage(str, Enum):
        """SRD 5.2.1 lineages with attunement-gated items (hardcoded, currently dwarf only)."""
        DWARF = "dwarf"

    class Capability(str, Enum):
        """Cross-class capability tags for attunement (hardcoded, currently spellcaster only)."""
        SPELLCASTER = "spellcaster"

    @dataclass(frozen=True)
    class AttunementClass:
        value: "model.SrdClass"

    @dataclass(frozen=True)
    class AttunementLineage:
        value: "model.SrdLineage"

    @dataclass(frozen=True)
    class AttunementCapability:
        value: "model.Capability"

    @dataclass(frozen=True)
    class AttunementAttunedTo:
        """attunement_requirement::attuned_to(string(slug)) -- slug refs another magic_item."""
        slug: str

    # AttunementRequirement = Union[AttunementAny, AttunementClass, AttunementLineage,
    #                               AttunementCapability, AttunementAttunedTo]

    # ----- DOMMF: logic<T> combinators -----
    # Generic over leaf T; for attunement, leaf is AttunementRequirement.
    # `values` is a tuple (not list) so the dataclass stays hashable.

    @dataclass(frozen=True)
    class Is:
        """logic<T>::is(T) -- a single leaf value."""
        value: Any

    @dataclass(frozen=True)
    class In:
        """logic<T>::in(vec<T>) -- set membership; sugar for or(is(T)...). Empty=false."""
        values: tuple = ()

    @dataclass(frozen=True)
    class NotIn:
        """logic<T>::not_in(vec<T>) -- set non-membership. Empty=true."""
        values: tuple = ()

    @dataclass(frozen=True)
    class Not:
        """logic<T>::not(logic<T>)."""
        value: Any

    @dataclass(frozen=True)
    class And:
        """logic<T>::and(vec<logic<T>>) -- conjunction. Empty=true."""
        values: tuple = ()

    @dataclass(frozen=True)
    class Or:
        """logic<T>::or(vec<logic<T>>) -- disjunction. Empty=false."""
        values: tuple = ()

    # Logic[T] = Union[Is, In, NotIn, Not, And, Or]

    # ----- DOMMF: fuzz<T> -----

    @dataclass(frozen=True)
    class Hard:
        """fuzz<T>::hard(T) -- structured per the inner type."""
        value: Any

    @dataclass(frozen=True)
    class Soft:
        """fuzz<T>::soft(string(md)) -- prose escape hatch."""
        value: str

    # Fuzz[T] = Union[Hard, Soft]

    # ----- DOMMF: dice notation -----

    class Die(str, Enum):
        """SRD dice sizes used in roll expressions."""
        D3 = "d3"
        D4 = "d4"
        D6 = "d6"
        D8 = "d8"
        D10 = "d10"
        D12 = "d12"
        D20 = "d20"
        D100 = "d100"

    class Op(str, Enum):
        """Operators used in roll_modifier."""
        ADD = "add"
        SUBTRACT = "subtract"
        MULTIPLY = "multiply"
        DIVIDE = "divide"

    @dataclass(frozen=True)
    class RollModifier:
        op: "model.Op"
        value: int  # u32 per DOMMF; value is the literal that follows the op

    @dataclass(frozen=True)
    class Roll:
        """Roll : (n d die) [op value]. Encodes 'XdN', 'XdN + Y', 'XdN - Y', 'XdN x Y'."""
        n: int  # u32
        d: "model.Die"
        modifier: Optional["model.RollModifier"] = None

    # ----- DOMMF: unum / inum variant dataclasses -----
    #
    # Both unions share the same structural variants: Fixed (a literal integer)
    # and Rolled (a dice expression). Python can't type-system-distinguish unum
    # from inum at the variant level, so we collapse to a single Fixed/Rolled
    # pair. The unum semantics (clamp underflow to 0, floor fractional) and
    # inum semantics (floor fractional) apply at evaluation time, which is a
    # consumer concern; the data layer just carries the structured value.

    @dataclass(frozen=True)
    class Fixed:
        """unum/inum::fixed(int) -- a literal integer."""
        value: int

    @dataclass(frozen=True)
    class Rolled:
        """unum/inum::rolled(roll) -- a dice-roll expression."""
        value: "model.Roll"

    # Unum = Union[Fixed, Rolled]  -- clamp underflow to 0, floor fractional
    # Inum = Union[Fixed, Rolled]  -- floor fractional (no underflow rule)

    @dataclass
    class MagicItemVariant:
        slug: str
        name: str
        rarity: "Optional[model.Rarity]" = None
        description: Optional[str] = None
        # charges: option<unum>
        charges: Optional[Union["model.Fixed", "model.Rolled"]] = None

    @dataclass
    class MagicItem:
        slug: str
        name: str
        category: str
        description: str
        category_description: Optional[str] = None
        rarity: "Optional[model.Rarity]" = None
        variants: list = field(default_factory=list)
        # attunement: option<fuzz<logic<attunement_requirement>>>
        # None     = no attunement required
        # Hard(L)  = structured logic tree over AttunementRequirement variants
        # Soft(s)  = prose escape (markup-incomplete or unrecognized clause)
        attunement: Optional[Union["model.Hard", "model.Soft"]] = None
        special_rules: list = field(default_factory=list)
        creature: Optional["model.Creature"] = None
        # charges: option<unum>
        charges: Optional[Union["model.Fixed", "model.Rolled"]] = None




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
        _XP_INIT_MOD = _letree.XPath('.//span[@class="creature-initiative-modifier"]/text()')
        _XP_PASSIVE_INIT = _letree.XPath('.//span[@class="creature-passive-initiative"]')
        _XP_PASSIVE_PERC = _letree.XPath('.//span[@class="creature-passive-perception"]/text()')
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
        def initiative_modifier(self) -> Optional[int]:
            r = self._XP_INIT_MOD(self._element)
            return int(r[0]) if r else None

        @property
        def passive_perception(self) -> int:
            r = self._XP_PASSIVE_PERC(self._element)
            return int(r[0]) if r else 0

        @property
        def passive_initiative(self) -> Optional[int]:
            els = self._XP_PASSIVE_INIT(self._element)
            if not els:
                return None
            el = els[0]
            # data-exceptional="erratum": trust data-fix over the preserved (erroneous) source text
            if "erratum" in (el.get("data-exceptional") or "").split() and el.get("data-fix") is not None:
                return int(el.get("data-fix"))
            return int((el.text or "").strip())

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
                initiative_modifier=self.initiative_modifier,
                passive_perception=self.passive_perception,
                passive_initiative=self.passive_initiative,
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
                "initiative_modifier": self.initiative_modifier,
                "passive_perception": self.passive_perception,
                "passive_initiative": self.passive_initiative,
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
        """A special-rule entry — dt heading + dd body. Same model whether
        the dl lives inside a magic-item-special section or a spell-special section.
        slug derives from the variant-name span if present (for joinability with
        the variants list), otherwise from slugify(dt text). heading is the full
        dt text; content is md of dd."""

        _XP_VARIANT_NAME = _letree.XPath('.//*[@class="magic-item-variant-name"]/text()')

        def __init__(self, dt: _Element, dd: _Element):
            self._dt = dt
            self._dd = dd

        @property
        def slug(self) -> str:
            # Manual override: data-exceptional="reslug" + data-slug="..."
            exceptional = self._dt.get("data-exceptional") or ""
            if "reslug" in exceptional.split():
                manual = self._dt.get("data-slug")
                if manual:
                    return manual
            # Prefer variant-name span text for joinability with variants
            r = self._XP_VARIANT_NAME(self._dt)
            if r:
                return _slugify(r[0])
            return _slugify(self.heading)

        @property
        def heading(self) -> str:
            return (self._dt.text_content() or "").strip()

        @property
        def content(self) -> str:
            return _md_content(self._dd)

        def to_model(self) -> "model.SpecialRules":
            return model.SpecialRules(
                slug=self.slug, heading=self.heading, content=self.content,
            )

        def to_dict(self) -> dict:
            return {
                "slug": self.slug,
                "heading": self.heading,
                "content": self.content,
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
        _XP_SPECIAL_DTS = _letree.XPath(
            './div[@class="spell-specials"]/section[@class="spell-special"]/dl/dt'
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
            dts = self._XP_SPECIAL_DTS(self._element)
            pairs = []
            for dt in dts:
                dd = dt.getnext()
                if dd is not None and dd.tag == "dd":
                    pairs.append(query.SpecialRules(dt, dd))
            return pairs

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

        _XP_NAME = _letree.XPath('.//*[@class="magic-item-name"]/text()')
        _XP_CATEGORY = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-category"]/text()'
        )
        _XP_CATEGORY_DESC = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-category-description"]/text()'
        )
        _XP_RARITY = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-rarity"]/text()'
        )
        _XP_ATTUNEMENT_SPAN = _letree.XPath(
            './p[@class="magic-item-general"]/span[@class="magic-item-attunement"]'
        )
        _XP_DESCRIPTION = _letree.XPath('./div[@class="magic-item-description"]')
        _XP_SPECIAL_DTS = _letree.XPath(
            './div[@class="magic-item-specials"]/section[@class="magic-item-special"]/dl/dt'
        )
        _XP_VARIANT_NAME_SPANS = _letree.XPath('.//*[@class="magic-item-variant-name"]')
        _XP_VARIANT_RARITY_SPANS = _letree.XPath('.//*[@class="magic-item-variant-rarity"]/text()')
        _XP_EMBEDDED_CREATURE = _letree.XPath(
            './div[@class="magic-item-description"]//section[@class="creature"] | '
            './div[@class="magic-item-specials"]//section[@class="creature"]'
        )
        _XP_ALL_CHARGES_SPANS = _letree.XPath(
            './/span[@class="magic-item-charges"]'
        )

        def __init__(self, element: _Element):
            self._element = element

        @property
        def slug(self) -> str:
            return self._element.get("id", "").removeprefix("magic-item-")

        @property
        def name(self) -> str:
            r = self._XP_NAME(self._element)
            return r[0].strip() if r else ""

        @property
        def category(self) -> str:
            r = self._XP_CATEGORY(self._element)
            return r[0] if r else ""

        @property
        def category_description(self) -> Optional[str]:
            r = self._XP_CATEGORY_DESC(self._element)
            return r[0] if r else None

        @property
        def rarity(self) -> Optional[str]:
            r = self._XP_RARITY(self._element)
            return _normalize_rarity(r[0]) if r else None

        @property
        def attunement(self):
            """Return Optional[Union[model.Hard, model.Soft]] per option<fuzz<logic<attunement_requirement>>>.

            None  -- no attunement span present (item does not require attunement).
            Hard  -- span carries data-exceptional="logic" and a parseable data-logic.
            Soft  -- span lacks the data-exceptional="logic" flag, or data-logic
                     fails to parse. Holds the visible prose for consumer fallback.
            """
            spans = self._XP_ATTUNEMENT_SPAN(self._element)
            if not spans:
                return None
            span = spans[0]
            text = (span.text or "").strip()
            data_exc = (span.get("data-exceptional") or "").split()
            if "logic" in data_exc:
                try:
                    logic = _parse_attunement_logic(span.get("data-logic", ""))
                    return model.Hard(logic)
                except _AttunementParseError:
                    return model.Soft(text)
            return model.Soft(text)

        @property
        def charges(self):
            """Return Optional[Union[model.Fixed, model.Rolled]] per option<unum>.

            None    -- no parent-level <span class="magic-item-charges">.
            Fixed   -- integer literal in the span (e.g. '7' -> Fixed(7)).
            Rolled  -- dice expression (e.g. '1d8 + 1' -> Rolled(Roll(...))).

            Searches the entire section but filters out variant-level spans
            (those whose containing block also holds a magic-item-variant-name).
            Examples:
              wand-of-magic-missiles: span in description div -> parent
              nine-lives-stealer:     span in a non-variant special_rule dd -> parent
              figurine-of-wondrous-power: span in a dd whose preceding dt
                                          has a variant-name -> variant, not parent
            """
            for span in self._XP_ALL_CHARGES_SPANS(self._element):
                if not _charges_span_is_variant_level(span):
                    return _parse_charges(span.text or "")
            return None

        @property
        def variants(self) -> List[dict]:
            """Extract variants from magic-item-variant-name spans throughout the section.
            Pairs positionally with magic-item-variant-rarity spans in document order.
            description is populated based on the span's enclosing context:
              - inside <td>: none (table data)
              - inside <h4>: none
              - inside <dt>: matching <dd>'s markdown
              - inside <span class="subject"> within <p>: prose after the subject span
            charges is extracted from any <span class="magic-item-charges"> inside
            the variant's containing block (dd / p / td / h4). None when absent.
            """
            name_spans = self._XP_VARIANT_NAME_SPANS(self._element)
            rarity_texts = list(self._XP_VARIANT_RARITY_SPANS(self._element))
            variants = []
            for i, span in enumerate(name_spans):
                vname = (span.text_content() or "").strip()
                vslug = _slugify(vname)
                vrarity = _normalize_rarity(rarity_texts[i]) if i < len(rarity_texts) else None
                vdescription = self._extract_variant_description(span)
                vcharges = self._extract_variant_charges(span)
                variants.append({
                    "slug": vslug,
                    "name": vname,
                    "rarity": vrarity,
                    "description": vdescription,
                    "charges": vcharges,
                })
            return variants

        @staticmethod
        def _extract_variant_charges(span: _Element):
            """Find a <span class="magic-item-charges"> inside the variant's
            containing block (the dd matching a dt-variant, or the p containing
            a subject-variant). Returns Fixed/Rolled or None.
            """
            block = span
            while block is not None:
                if block.tag == "dt":
                    dd = block.getnext()
                    if dd is not None and dd.tag == "dd":
                        charges_spans = dd.xpath('.//span[@class="magic-item-charges"]/text()')
                        if charges_spans:
                            return _parse_charges(charges_spans[0])
                    return None
                if block.tag in ("td", "h4"):
                    return None
                if block.tag == "p":
                    # For subject-paragraph variants, look in the post-subject
                    # tail and following siblings of the subject span for a
                    # charges span.
                    charges_spans = block.xpath('.//span[@class="magic-item-charges"]/text()')
                    if charges_spans:
                        return _parse_charges(charges_spans[0])
                    return None
                block = block.getparent()
            return None

        @staticmethod
        def _extract_variant_description(span: _Element) -> Optional[str]:
            """Find the variant's description based on span's enclosing context."""
            # Walk up to find the containing block element
            block = span
            while block is not None:
                if block.tag in ("td", "h4"):
                    return None
                if block.tag == "dt":
                    # Description is the matching following-sibling dd
                    dd = block.getnext()
                    if dd is not None and dd.tag == "dd":
                        return _md_content(dd) or None
                    return None
                if block.tag == "p":
                    # If the span is inside a span class="subject" within a p,
                    # description is the prose after the subject span
                    # Otherwise, no description
                    subject = span.getparent() if span.getparent() is not None else None
                    # Subject may be the span's direct parent (or higher)
                    cur = span.getparent()
                    while cur is not None and cur is not block:
                        if cur.tag == "span" and cur.get("class") == "subject":
                            subject = cur
                            break
                        cur = cur.getparent()
                    if subject is not None and subject.tag == "span" and subject.get("class") == "subject":
                        # Take the p's text content after the subject span
                        post_text = (subject.tail or "")
                        for sib in subject.itersiblings():
                            post_text += _md_block(sib) or sib.text_content() or ""
                            if sib.tail:
                                post_text += sib.tail
                        return post_text.strip() or None
                    return None
                block = block.getparent()
            return None

        @property
        def description(self) -> str:
            descs = self._XP_DESCRIPTION(self._element)
            return _md_children(descs[0]) if descs else ""

        @property
        def special_rules(self) -> "List[query.SpecialRules]":
            """Iterate dt/dd pairs inside <section class="magic-item-special">/<dl>.
            Each dt's following-sibling dd forms a SpecialRules pair."""
            dts = self._XP_SPECIAL_DTS(self._element)
            pairs = []
            for dt in dts:
                dd = dt.getnext()
                if dd is not None and dd.tag == "dd":
                    pairs.append(query.SpecialRules(dt, dd))
            return pairs

        @property
        def creature(self) -> "Optional[query.Creature]":
            r = self._XP_EMBEDDED_CREATURE(self._element)
            return query.Creature(r[0]) if r else None

        def to_model(self) -> "model.MagicItem":
            embedded = self.creature
            rarity_val = self.rarity
            return model.MagicItem(
                slug=self.slug,
                name=self.name,
                category=self.category,
                category_description=self.category_description,
                rarity=model.Rarity(rarity_val) if rarity_val else None,
                variants=[
                    model.MagicItemVariant(
                        slug=v["slug"], name=v["name"],
                        rarity=model.Rarity(v["rarity"]) if v["rarity"] else None,
                        description=v["description"],
                        charges=v["charges"],
                    )
                    for v in self.variants
                ],
                attunement=self.attunement,
                description=self.description,
                special_rules=[s.to_model() for s in self.special_rules],
                creature=embedded.to_model() if embedded else None,
                charges=self.charges,
            )

        def to_dict(self) -> dict:
            embedded = self.creature
            return {
                "slug": self.slug,
                "name": self.name,
                "category": self.category,
                "category_description": self.category_description,
                "rarity": self.rarity,
                "variants": [
                    {**v, "charges": _charges_to_dict(v["charges"])}
                    for v in self.variants
                ],
                "attunement": _attunement_to_dict(self.attunement),
                "description": self.description,
                "special_rules": [s.to_dict() for s in self.special_rules],
                "creature": embedded.to_dict() if embedded else None,
                "charges": _charges_to_dict(self.charges),
            }

        def __repr__(self) -> str:
            cd = f" ({self.category_description})" if self.category_description else ""
            rarity = self.rarity or "—"
            n_variants = len(self.variants)
            v_note = f", {n_variants} variant(s)" if n_variants else ""
            return f"<query.MagicItem {self.slug!r}: {self.category}{cd}, {rarity}{v_note}>"


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
# Attunement: data-logic mini-language parser / renderer / dict serializer
# ============================================================================
#
# The data-logic attribute on <span class="magic-item-attunement"> (paired with
# data-exceptional="logic") encodes a logic<attunement_requirement> tree as a
# string in DOMMF-mirroring notation. Examples:
#
#     is(any)
#     is(class(wizard))
#     is(capability(spellcaster))
#     in([class(bard), class(cleric), class(druid)])
#     in([lineage(dwarf), attuned_to(belt-of-dwarvenkind)])
#
# Grammar:
#
#     logic       := is( requirement )
#                  | in( [ requirement, ... ] )
#                  | not_in( [ requirement, ... ] )
#                  | not( logic )
#                  | and( [ logic, ... ] )
#                  | or( [ logic, ... ] )
#     requirement := any
#                  | class( srd_class )
#                  | lineage( srd_lineage )
#                  | capability( capability )
#                  | attuned_to( slug )
#
# Whitespace flexible. Identifiers and slugs lex as [a-z0-9_-]+ (slugs include
# hyphens; the bare-identifier form is unambiguous given the surrounding
# punctuation).
#
# Parse failures raise _AttunementParseError; the query layer catches and falls
# back to Soft(visible_text).


class _AttunementParseError(ValueError):
    """Raised when data-logic cannot be parsed."""


_ATT_TOKEN_RE = re.compile(r'[a-z0-9_-]+|[(){}\[\],]')


def _tokenize_attunement(s: str) -> List[str]:
    """Split a data-logic string into tokens. Whitespace is the separator."""
    tokens: List[str] = []
    pos = 0
    n = len(s)
    while pos < n:
        c = s[pos]
        if c.isspace():
            pos += 1
            continue
        m = _ATT_TOKEN_RE.match(s, pos)
        if not m:
            raise _AttunementParseError(
                f"unexpected character at position {pos}: {c!r}"
            )
        tokens.append(m.group(0))
        pos = m.end()
    return tokens


# Lookup sets for enum identifier validation
_ATT_CLASS_NAMES = {c.value for c in model.SrdClass}
_ATT_LINEAGE_NAMES = {l.value for l in model.SrdLineage}
_ATT_CAPABILITY_NAMES = {c.value for c in model.Capability}


def _parse_attunement_logic(s: str):
    """Parse a data-logic string into a logic<attunement_requirement> tree.

    Returns a model.Is/In/NotIn/Not/And/Or holding model.Attunement* leaves.
    Raises _AttunementParseError on any malformed input.
    """
    tokens = _tokenize_attunement(s)
    if not tokens:
        raise _AttunementParseError("empty data-logic")
    idx = [0]

    def peek():
        return tokens[idx[0]] if idx[0] < len(tokens) else None

    def consume(expected=None):
        t = peek()
        if t is None:
            raise _AttunementParseError(
                f"unexpected end of input (expected {expected!r})"
                if expected is not None
                else "unexpected end of input"
            )
        if expected is not None and t != expected:
            raise _AttunementParseError(f"expected {expected!r}, got {t!r}")
        idx[0] += 1
        return t

    def parse_vec_of(parser):
        """Parse '[ item, item, ... ]' using `parser` for each item.
        Trailing comma is not allowed."""
        consume("[")
        items = []
        if peek() != "]":
            items.append(parser())
            while peek() == ",":
                consume(",")
                items.append(parser())
        consume("]")
        return tuple(items)

    def parse_requirement():
        ctor = consume()
        if ctor == "any":
            return model.AttunementAny()
        if ctor == "class":
            consume("(")
            name = consume()
            consume(")")
            if name not in _ATT_CLASS_NAMES:
                raise _AttunementParseError(f"unknown srd_class: {name!r}")
            return model.AttunementClass(model.SrdClass(name))
        if ctor == "lineage":
            consume("(")
            name = consume()
            consume(")")
            if name not in _ATT_LINEAGE_NAMES:
                raise _AttunementParseError(f"unknown srd_lineage: {name!r}")
            return model.AttunementLineage(model.SrdLineage(name))
        if ctor == "capability":
            consume("(")
            name = consume()
            consume(")")
            if name not in _ATT_CAPABILITY_NAMES:
                raise _AttunementParseError(f"unknown capability: {name!r}")
            return model.AttunementCapability(model.Capability(name))
        if ctor == "attuned_to":
            consume("(")
            slug = consume()
            consume(")")
            return model.AttunementAttunedTo(slug)
        raise _AttunementParseError(f"unknown requirement constructor: {ctor!r}")

    def parse_logic():
        op = consume()
        if op == "is":
            consume("(")
            r = parse_requirement()
            consume(")")
            return model.Is(r)
        if op == "in":
            consume("(")
            vals = parse_vec_of(parse_requirement)
            consume(")")
            return model.In(vals)
        if op == "not_in":
            consume("(")
            vals = parse_vec_of(parse_requirement)
            consume(")")
            return model.NotIn(vals)
        if op == "not":
            consume("(")
            inner = parse_logic()
            consume(")")
            return model.Not(inner)
        if op == "and":
            consume("(")
            vals = parse_vec_of(parse_logic)
            consume(")")
            return model.And(vals)
        if op == "or":
            consume("(")
            vals = parse_vec_of(parse_logic)
            consume(")")
            return model.Or(vals)
        raise _AttunementParseError(f"unknown logic operator: {op!r}")

    result = parse_logic()
    if idx[0] != len(tokens):
        raise _AttunementParseError(f"trailing tokens: {tokens[idx[0]:]}")
    return result


def _render_attunement_requirement(req) -> str:
    if isinstance(req, model.AttunementAny):
        return "any"
    if isinstance(req, model.AttunementClass):
        return f"class({req.value.value})"
    if isinstance(req, model.AttunementLineage):
        return f"lineage({req.value.value})"
    if isinstance(req, model.AttunementCapability):
        return f"capability({req.value.value})"
    if isinstance(req, model.AttunementAttunedTo):
        return f"attuned_to({req.slug})"
    raise ValueError(f"not an attunement_requirement: {req!r}")


def _render_attunement_logic(logic) -> str:
    """Render a logic<attunement_requirement> tree back to data-logic string form."""
    if isinstance(logic, model.Is):
        return f"is({_render_attunement_requirement(logic.value)})"
    if isinstance(logic, model.In):
        items = ", ".join(_render_attunement_requirement(r) for r in logic.values)
        return f"in([{items}])"
    if isinstance(logic, model.NotIn):
        items = ", ".join(_render_attunement_requirement(r) for r in logic.values)
        return f"not_in([{items}])"
    if isinstance(logic, model.Not):
        return f"not({_render_attunement_logic(logic.value)})"
    if isinstance(logic, model.And):
        items = ", ".join(_render_attunement_logic(l) for l in logic.values)
        return f"and([{items}])"
    if isinstance(logic, model.Or):
        items = ", ".join(_render_attunement_logic(l) for l in logic.values)
        return f"or([{items}])"
    raise ValueError(f"not a logic value: {logic!r}")


def _attunement_requirement_to_dict(req) -> dict:
    if isinstance(req, model.AttunementAny):
        return {"kind": "any"}
    if isinstance(req, model.AttunementClass):
        return {"kind": "class", "value": req.value.value}
    if isinstance(req, model.AttunementLineage):
        return {"kind": "lineage", "value": req.value.value}
    if isinstance(req, model.AttunementCapability):
        return {"kind": "capability", "value": req.value.value}
    if isinstance(req, model.AttunementAttunedTo):
        return {"kind": "attuned_to", "value": req.slug}
    raise ValueError(f"not an attunement_requirement: {req!r}")


def _logic_to_dict(logic) -> dict:
    if isinstance(logic, model.Is):
        return {"kind": "is", "value": _attunement_requirement_to_dict(logic.value)}
    if isinstance(logic, model.In):
        return {"kind": "in", "values": [_attunement_requirement_to_dict(r) for r in logic.values]}
    if isinstance(logic, model.NotIn):
        return {"kind": "not_in", "values": [_attunement_requirement_to_dict(r) for r in logic.values]}
    if isinstance(logic, model.Not):
        return {"kind": "not", "value": _logic_to_dict(logic.value)}
    if isinstance(logic, model.And):
        return {"kind": "and", "values": [_logic_to_dict(l) for l in logic.values]}
    if isinstance(logic, model.Or):
        return {"kind": "or", "values": [_logic_to_dict(l) for l in logic.values]}
    raise ValueError(f"not a logic value: {logic!r}")


def _attunement_to_dict(attunement) -> Optional[dict]:
    """Serialize Optional[Fuzz[Logic[AttunementRequirement]]] to JSON-safe dict."""
    if attunement is None:
        return None
    if isinstance(attunement, model.Hard):
        return {"kind": "hard", "value": _logic_to_dict(attunement.value)}
    if isinstance(attunement, model.Soft):
        return {"kind": "soft", "value": attunement.value}
    raise ValueError(f"not a fuzz value: {attunement!r}")


# ============================================================================
# Roll / Charges: parser / renderer / dict serializer
# ============================================================================
#
# The <span class="magic-item-charges"> content is the canonical machine-readable
# form: a literal integer ('7', '50') or a dice expression ('1d3', '1d8 + 1',
# '1d10 x 10'). No data-* payload attribute is needed because the source token
# IS the structured form; the dispatcher checks for 'd' to choose Fixed vs Rolled.
#
# Roll grammar (matches every shape in SRD 5.2.1):
#
#     roll        := dice [ ws op ws integer ]
#     dice        := integer 'd' integer
#     op          := '+' | '-' | 'x'
#
# Only one modifier per expression; no chained dice (no '1d6 + 1d8'); no parens.
# Op character is ASCII 'x' (Unicode '×' was normalized to ASCII in earlier passes).
#
# Note on coercion: this layer produces the structured form only. The unum/inum
# semantic rules (clamp underflow to 0; floor fractional results) apply at
# evaluation time -- a consumer concern. The data extracted here is purely
# structural; evaluating a Rolled to a concrete integer is the caller's job.


class _RollParseError(ValueError):
    """Raised when a roll expression cannot be parsed."""


# Strict roll grammar — leading int, 'd', int, optional ' op N'
_ROLL_RE = re.compile(
    r'^\s*(\d+)d(\d+)(?:\s*([+\-x])\s*(\d+))?\s*$'
)

_DIE_SIZES = {3, 4, 6, 8, 10, 12, 20, 100}
_OP_CHAR_TO_ENUM = {
    "+": "add",
    "-": "subtract",
    "x": "multiply",
    # No source token for divide; reserved in the enum per spec choice.
}


def _parse_roll(s: str) -> "model.Roll":
    """Parse a roll expression string into a model.Roll.

    Examples:
        "1d6"        -> Roll(1, Die.D6, None)
        "2d6 + 3"    -> Roll(2, Die.D6, RollModifier(Op.ADD, 3))
        "1d10 x 10"  -> Roll(1, Die.D10, RollModifier(Op.MULTIPLY, 10))

    Raises _RollParseError on any malformed input or unknown die size.
    """
    m = _ROLL_RE.match(s)
    if not m:
        raise _RollParseError(f"not a roll expression: {s!r}")
    n_str, d_str, op_char, val_str = m.group(1), m.group(2), m.group(3), m.group(4)
    n = int(n_str)
    d_size = int(d_str)
    if d_size not in _DIE_SIZES:
        raise _RollParseError(
            f"unknown die size d{d_size} (allowed: {sorted(_DIE_SIZES)})"
        )
    die = model.Die(f"d{d_size}")
    modifier = None
    if op_char is not None:
        op_name = _OP_CHAR_TO_ENUM.get(op_char)
        if op_name is None:
            raise _RollParseError(f"unknown op char: {op_char!r}")
        modifier = model.RollModifier(op=model.Op(op_name), value=int(val_str))
    return model.Roll(n=n, d=die, modifier=modifier)


def _parse_charges(s: str) -> Union["model.Fixed", "model.Rolled"]:
    """Dispatch a charges-span text to Fixed(int) or Rolled(Roll).

    Presence of 'd' in the token selects Rolled; otherwise Fixed. Raises
    _RollParseError if the Rolled path fails to parse, ValueError if the
    Fixed path encounters a non-integer.
    """
    s = s.strip()
    if "d" in s:
        return model.Rolled(_parse_roll(s))
    return model.Fixed(int(s))


_OP_ENUM_TO_CHAR = {v: k for k, v in _OP_CHAR_TO_ENUM.items()}


def _render_roll(r: "model.Roll") -> str:
    """Render a Roll back to source-form string. Round-trips with _parse_roll."""
    die_size = r.d.value[1:]  # strip 'd' prefix
    base = f"{r.n}d{die_size}"
    if r.modifier is None:
        return base
    op_char = _OP_ENUM_TO_CHAR.get(r.modifier.op.value)
    if op_char is None:
        raise ValueError(f"no render symbol for op {r.modifier.op}")
    return f"{base} {op_char} {r.modifier.value}"


def _render_charges(c) -> str:
    """Render a Fixed/Rolled back to source-form string."""
    if isinstance(c, model.Fixed):
        return str(c.value)
    if isinstance(c, model.Rolled):
        return _render_roll(c.value)
    raise ValueError(f"not a charges value: {c!r}")


def _roll_to_dict(r: "model.Roll") -> dict:
    """Tagged-record JSON shape for a Roll."""
    mod = None
    if r.modifier is not None:
        mod = {"op": r.modifier.op.value, "value": r.modifier.value}
    return {"n": r.n, "d": r.d.value, "modifier": mod}


def _charges_to_dict(c) -> Optional[dict]:
    """Serialize Optional[Union[Fixed, Rolled]] to JSON-safe tagged dict."""
    if c is None:
        return None
    if isinstance(c, model.Fixed):
        return {"kind": "fixed", "value": c.value}
    if isinstance(c, model.Rolled):
        return {"kind": "rolled", "value": _roll_to_dict(c.value)}
    raise ValueError(f"not a charges value: {c!r}")


def _charges_span_is_variant_level(span: _Element) -> bool:
    """Return True if a magic-item-charges span belongs to a variant rather than
    the parent magic_item. A span is variant-level if ANY of its enclosing
    block-level ancestors (dd/p/td/h4) shares scope with a
    magic-item-variant-name span:

      - dd: the preceding dt holds a variant-name span
      - p:  the p contains a variant-name span (subject-paragraph variants)
      - td: the td contains a variant-name span (table-row variants)
      - h4: the h4 contains a variant-name span (heading variants -- rare)

    Walks up through nested blocks so deeply-nested cases (e.g., figurine
    Ivory Goats variant whose '24 charges' span sits in a nested dt/dd inside
    the variant's outer dd) are correctly classified as variant-level.
    """
    cur = span.getparent()
    while cur is not None:
        tag = getattr(cur, 'tag', None)
        if tag == "section":
            # Reached the magic-item section; no variant-containment above this
            break
        if tag == "dd":
            dt = cur.getprevious()
            if dt is not None and getattr(dt, 'tag', None) == "dt":
                if dt.xpath('.//span[@class="magic-item-variant-name"]'):
                    return True
            # Otherwise keep walking up; an outer dd might be variant-scoped
        elif tag in ("p", "td", "h4"):
            if cur.xpath('.//span[@class="magic-item-variant-name"]'):
                return True
        cur = cur.getparent()
    return False




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
            "Query SRDOM (SRD 5.2.1 as structured HTML, 5E compatible). "
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
    def test_invisible_stalker_initiative_and_passives(doc):
        # Normal creature; score carries +5 from the Invisible condition (Advantage on Initiative).
        c = doc.creatures["invisible-stalker"]
        assert c.initiative_modifier == 7, f"init_mod {c.initiative_modifier!r}"
        assert c.passive_initiative == 22, f"passive_init {c.passive_initiative!r}"
        assert c.passive_perception == 18, f"passive_perc {c.passive_perception!r}"

    @staticmethod
    def test_gray_ooze_initiative_erratum(doc):
        # SRD prints score 13 (erratum, preserved in source text); data-fix corrects to 8.
        c = doc.creatures["gray-ooze"]
        assert c.initiative_modifier == -2, f"init_mod {c.initiative_modifier!r}"
        assert c.passive_initiative == 8, f"passive_init {c.passive_initiative!r} (should be data-fix 8, not 13)"
        assert c.passive_perception == 8, f"passive_perc {c.passive_perception!r}"

    @staticmethod
    def test_conjured_template_omits_initiative(doc):
        # Conjured/scalable stat blocks have no Initiative entry -> None; passive perception still present.
        c = doc.creatures["animated-object"]
        assert c.initiative_modifier is None, f"init_mod {c.initiative_modifier!r}"
        assert c.passive_initiative is None, f"passive_init {c.passive_initiative!r}"
        assert c.passive_perception == 6, f"passive_perc {c.passive_perception!r}"

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
        assert m.rarity == "legendary"
        # Holy Avenger requires Paladin attunement
        assert m.attunement == model.Hard(model.Is(model.AttunementClass(model.SrdClass.PALADIN)))

    @staticmethod
    def test_weapon_has_variants(doc):
        m = doc.magic_items["weapon"]
        assert m.rarity is None, f"expected parent.rarity=None for all-variants weapon, got {m.rarity!r}"
        assert len(m.variants) == 3
        slugs = [v["slug"] for v in m.variants]
        assert slugs == ["1", "2", "3"], f"expected slugs ['1','2','3'], got {slugs}"
        names = [v["name"] for v in m.variants]
        assert names == ["+1", "+2", "+3"], f"got names {names}"
        rarities = [v["rarity"] for v in m.variants]
        assert rarities == ["uncommon", "rare", "very_rare"], f"got rarities {rarities}"

    @staticmethod
    def test_potion_of_healing_singularized(doc):
        m = doc.magic_items["potion-of-healing"]
        assert m.name == "Potion of Healing", f"expected canonical name, got {m.name!r}"
        assert m.rarity == "common", f"expected parent.rarity=common, got {m.rarity!r}"
        assert len(m.variants) == 3, f"expected 3 deviation variants, got {len(m.variants)}"
        names = [v["name"] for v in m.variants]
        assert names == ["greater", "superior", "supreme"], f"got names {names}"

    @staticmethod
    def test_belt_of_giant_strength_variants(doc):
        m = doc.magic_items["belt-of-giant-strength"]
        assert m.rarity is None
        assert len(m.variants) == 5, f"expected 5 variants, got {len(m.variants)}"
        names = [v["name"] for v in m.variants]
        assert "hill" in names and "frost or stone" in names and "storm" in names

    @staticmethod
    def test_ioun_stone_variants_have_descriptions(doc):
        m = doc.magic_items["ioun-stone"]
        assert m.rarity is None
        assert len(m.variants) == 14, f"expected 14 ioun stones, got {len(m.variants)}"
        # All variants should have non-empty descriptions
        empty = [v["name"] for v in m.variants if not v["description"]]
        assert not empty, f"variants with empty descriptions: {empty}"

    @staticmethod
    def test_spell_scroll_variants(doc):
        m = doc.magic_items["spell-scroll"]
        assert m.rarity is None
        assert len(m.variants) == 10, f"expected 10 spell scrolls (Cantrip + 1-9), got {len(m.variants)}"
        names = [v["name"] for v in m.variants]
        assert names[0] == "Cantrip" and names[1] == "1" and names[-1] == "9"
        # Copying rule promoted to special_rules
        assert len(m.special_rules) == 1
        assert "Copying" in m.special_rules[0].heading

    @staticmethod
    def test_figurine_of_wondrous_power(doc):
        m = doc.magic_items["figurine-of-wondrous-power"]
        assert m.rarity is None
        # 9 outer variants (Bronze Griffon ... Silver Raven)
        # Plus 3 nested goats inside Ivory Goats — total span count is 12
        outer_names = ["Bronze Griffon", "Ebony Fly", "Golden Lions", "Ivory Goats",
                       "Marble Elephant", "Obsidian Steed", "Onyx Dog",
                       "Serpentine Owl", "Silver Raven"]
        actual_names = [v["name"] for v in m.variants]
        for n in outer_names:
            assert n in actual_names, f"missing variant {n!r}"
        # 9 special_rules from the dl/dt/dd
        assert len(m.special_rules) == 9, f"expected 9 special_rules, got {len(m.special_rules)}"
        # Giant Fly embedded inside Ebony Fly's dd
        assert m.creature is not None
        assert m.creature.name == "Giant Fly"

    @staticmethod
    def test_bag_of_tricks_variants(doc):
        m = doc.magic_items["bag-of-tricks"]
        assert m.rarity == "uncommon", f"expected uncommon, got {m.rarity!r}"
        assert len(m.variants) == 3
        names = [v["name"] for v in m.variants]
        assert names == ["Gray", "Rust", "Tan"]
        # Per-variant rarity is None (not repeated in source)
        rarities = [v["rarity"] for v in m.variants]
        assert all(r is None for r in rarities), f"expected all None, got {rarities}"
        # 3 special_rules
        assert len(m.special_rules) == 3

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

    @staticmethod
    def test_rarity_normalization(doc):
        assert _normalize_rarity("Common") == "common"
        assert _normalize_rarity("Very Rare") == "very_rare"
        assert _normalize_rarity("Legendary") == "legendary"
        assert _normalize_rarity("Rarity Varies") is None
        assert _normalize_rarity(None) is None
        assert _normalize_rarity("") is None

    @staticmethod
    def test_slugify_basics(doc):
        assert _slugify("Holy Avenger") == "holy-avenger"
        assert _slugify("Potion of Healing") == "potion-of-healing"
        assert _slugify("+1") == "1"
        assert _slugify("frost or stone") == "frost-or-stone"
        assert _slugify("Goat of Travail") == "goat-of-travail"
        # Apostrophes are stripped, not replaced
        assert _slugify("Giant's Bane") == "giants-bane"
        assert _slugify("Giants' Bane") == "giants-bane"
        # Curly apostrophe
        assert _slugify("Giant\u2019s Bane") == "giants-bane"

    @staticmethod
    def test_hammer_of_thunderbolts_reslug(doc):
        m = doc.magic_items["hammer-of-thunderbolts"]
        slugs = {sr.heading: sr.slug for sr in m.special_rules}
        # Umbrella heading reslugged to disambiguate from the sub-rule
        assert slugs["Giant's Bane"] == "giants-bane-rule"
        # Sub-rule keeps the natural (post-apostrophe-strip) slug, made explicit via reslug
        assert slugs["Giants' Bane"] == "giants-bane"
        assert slugs["Might of Giants"] == "might-of-giants"

    @staticmethod
    def test_slug_build_collapses_dashes(doc):
        assert _slug_build("Weapon", "+1") == "weapon-1"
        assert _slug_build("Potion of Healing", "greater") == "potion-of-healing-greater"
        # Empty parts get dropped
        assert _slug_build("foo", "", "bar") == "foo-bar"

    # ---- Attunement: 12 mapping tests pinning each unique SRD 5.2.1 clause ----

    @staticmethod
    def test_attunement_any(doc):
        """Bare 'Requires Attunement' (×118) → Hard(Is(AttunementAny))."""
        m = doc.magic_items["belt-of-dwarvenkind"]
        assert m.attunement == model.Hard(model.Is(model.AttunementAny()))

    @staticmethod
    def test_attunement_spellcaster(doc):
        """'…by a Spellcaster' (×7) → capability(spellcaster)."""
        m = doc.magic_items["pearl-of-power"]
        assert m.attunement == model.Hard(
            model.Is(model.AttunementCapability(model.Capability.SPELLCASTER))
        )

    @staticmethod
    def test_attunement_class_wizard(doc):
        m = doc.magic_items["hat-of-many-spells"]
        assert m.attunement == model.Hard(
            model.Is(model.AttunementClass(model.SrdClass.WIZARD))
        )

    @staticmethod
    def test_attunement_class_paladin(doc):
        m = doc.magic_items["holy-avenger"]
        assert m.attunement == model.Hard(
            model.Is(model.AttunementClass(model.SrdClass.PALADIN))
        )

    @staticmethod
    def test_attunement_class_druid(doc):
        m = doc.magic_items["staff-of-the-woodlands"]
        assert m.attunement == model.Hard(
            model.Is(model.AttunementClass(model.SrdClass.DRUID))
        )

    @staticmethod
    def test_attunement_cleric_or_paladin(doc):
        m = doc.magic_items["talisman-of-pure-good"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.CLERIC),
            model.AttunementClass(model.SrdClass.PALADIN),
        )))

    @staticmethod
    def test_attunement_bard_cleric_druid(doc):
        m = doc.magic_items["staff-of-healing"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.BARD),
            model.AttunementClass(model.SrdClass.CLERIC),
            model.AttunementClass(model.SrdClass.DRUID),
        )))

    @staticmethod
    def test_attunement_cleric_druid_paladin(doc):
        m = doc.magic_items["necklace-of-prayer-beads"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.CLERIC),
            model.AttunementClass(model.SrdClass.DRUID),
            model.AttunementClass(model.SrdClass.PALADIN),
        )))

    @staticmethod
    def test_attunement_sorcerer_warlock_wizard(doc):
        m = doc.magic_items["robe-of-the-archmagi"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.SORCERER),
            model.AttunementClass(model.SrdClass.WARLOCK),
            model.AttunementClass(model.SrdClass.WIZARD),
        )))

    @staticmethod
    def test_attunement_druid_sorcerer_warlock_wizard(doc):
        m = doc.magic_items["staff-of-fire"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.DRUID),
            model.AttunementClass(model.SrdClass.SORCERER),
            model.AttunementClass(model.SrdClass.WARLOCK),
            model.AttunementClass(model.SrdClass.WIZARD),
        )))

    @staticmethod
    def test_attunement_six_classes(doc):
        m = doc.magic_items["staff-of-charming"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementClass(model.SrdClass.BARD),
            model.AttunementClass(model.SrdClass.CLERIC),
            model.AttunementClass(model.SrdClass.DRUID),
            model.AttunementClass(model.SrdClass.SORCERER),
            model.AttunementClass(model.SrdClass.WARLOCK),
            model.AttunementClass(model.SrdClass.WIZARD),
        )))

    @staticmethod
    def test_attunement_dwarf_or_attuned_to_belt(doc):
        """The single heterogeneous clause: dwarf lineage OR attuned to Belt of Dwarvenkind."""
        m = doc.magic_items["dwarven-thrower"]
        assert m.attunement == model.Hard(model.In((
            model.AttunementLineage(model.SrdLineage.DWARF),
            model.AttunementAttunedTo("belt-of-dwarvenkind"),
        )))

    @staticmethod
    def test_attunement_none_when_unrequired(doc):
        """Magic items without an attunement span return None."""
        m = doc.magic_items["bag-of-holding"]
        assert m.attunement is None, (
            f"bag-of-holding should not require attunement, got {m.attunement!r}"
        )

    # ---- Attunement grammar: parser unit tests ----

    @staticmethod
    def test_attunement_parser_any(doc):
        assert _parse_attunement_logic("is(any)") == model.Is(model.AttunementAny())

    @staticmethod
    def test_attunement_parser_class(doc):
        assert _parse_attunement_logic("is(class(bard))") == \
            model.Is(model.AttunementClass(model.SrdClass.BARD))

    @staticmethod
    def test_attunement_parser_in_multi(doc):
        got = _parse_attunement_logic("in([class(cleric), class(druid), class(paladin)])")
        assert got == model.In((
            model.AttunementClass(model.SrdClass.CLERIC),
            model.AttunementClass(model.SrdClass.DRUID),
            model.AttunementClass(model.SrdClass.PALADIN),
        ))

    @staticmethod
    def test_attunement_parser_heterogeneous(doc):
        got = _parse_attunement_logic(
            "in([lineage(dwarf), attuned_to(belt-of-dwarvenkind)])"
        )
        assert got == model.In((
            model.AttunementLineage(model.SrdLineage.DWARF),
            model.AttunementAttunedTo("belt-of-dwarvenkind"),
        ))

    @staticmethod
    def test_attunement_parser_whitespace_tolerant(doc):
        """The parser should tolerate flexible whitespace around tokens."""
        compact = _parse_attunement_logic("in([class(bard),class(cleric)])")
        spaced = _parse_attunement_logic("in([ class(bard) , class(cleric) ])")
        verbose = _parse_attunement_logic("in (\n  [ class(bard),\n    class(cleric) ]\n)")
        assert compact == spaced == verbose

    @staticmethod
    def test_attunement_parser_rejects_malformed(doc):
        """Malformed inputs raise _AttunementParseError."""
        bad_inputs = [
            "",                              # empty
            "is",                            # missing parens
            "is(",                           # unclosed
            "is(class)",                     # missing class arg
            "is(class(unknown_class))",      # unknown enum variant
            "is(lineage(elf))",              # unknown lineage
            "is(capability(necromancer))",   # unknown capability
            "in(class(bard))",               # missing brackets
            "in([class(bard) class(cleric)])",  # missing comma
            "in([class(bard),])",            # trailing comma
            "is(class(bard)) extra",         # trailing tokens
            "garbage(stuff)",                # unknown op
        ]
        for s in bad_inputs:
            try:
                _parse_attunement_logic(s)
                raise AssertionError(f"expected parse error for {s!r}")
            except _AttunementParseError:
                pass  # expected

    # ---- Attunement renderer round-trip ----

    @staticmethod
    def test_attunement_render_roundtrip(doc):
        """Every canonical clause from the corpus round-trips through parse→render."""
        canonical = [
            "is(any)",
            "is(capability(spellcaster))",
            "is(class(wizard))",
            "is(class(paladin))",
            "is(class(druid))",
            "in([class(cleric), class(paladin)])",
            "in([class(bard), class(cleric), class(druid)])",
            "in([class(cleric), class(druid), class(paladin)])",
            "in([class(sorcerer), class(warlock), class(wizard)])",
            "in([class(druid), class(sorcerer), class(warlock), class(wizard)])",
            "in([class(bard), class(cleric), class(druid), class(sorcerer), class(warlock), class(wizard)])",
            "in([lineage(dwarf), attuned_to(belt-of-dwarvenkind)])",
        ]
        for s in canonical:
            assert _render_attunement_logic(_parse_attunement_logic(s)) == s, (
                f"round-trip failed for {s!r}"
            )

    # ---- Attunement to_dict serialization ----

    @staticmethod
    def test_attunement_to_dict_json_serializable(doc):
        """to_dict() output for items with attunement must be json-safe."""
        for slug in ["holy-avenger", "dwarven-thrower", "bag-of-holding",
                     "robe-of-the-archmagi", "belt-of-dwarvenkind"]:
            d = doc.magic_items[slug].to_dict()
            json.dumps(d)  # raises if not JSON-serializable

    @staticmethod
    def test_attunement_to_dict_shape(doc):
        """to_dict() encodes attunement as tagged-dict tree."""
        d = doc.magic_items["holy-avenger"].to_dict()
        assert d["attunement"] == {
            "kind": "hard",
            "value": {
                "kind": "is",
                "value": {"kind": "class", "value": "paladin"},
            },
        }
        d2 = doc.magic_items["dwarven-thrower"].to_dict()
        assert d2["attunement"] == {
            "kind": "hard",
            "value": {
                "kind": "in",
                "values": [
                    {"kind": "lineage", "value": "dwarf"},
                    {"kind": "attuned_to", "value": "belt-of-dwarvenkind"},
                ],
            },
        }
        # No attunement
        d3 = doc.magic_items["bag-of-holding"].to_dict()
        assert d3["attunement"] is None

    # ---- Attunement soft fallback ----

    @staticmethod
    def test_attunement_soft_fallback_on_missing_flag(doc):
        """Direct test of the parser contract: spans without data-exceptional='logic'
        fall to Soft(text). We synthesize one in-memory since the corpus has none."""
        from lxml import html as _h
        synthetic = _h.fromstring(
            '<section class="magic-item" id="magic-item-test">'
            '<h4>Test Item</h4>'
            '<p class="magic-item-general">'
            '  <span class="magic-item-category">Wondrous Item</span>, '
            '  <span class="magic-item-rarity">Common</span> '
            '  (<span class="magic-item-attunement">Requires Attunement by a Squirrel</span>)'
            '</p>'
            '<div class="magic-item-description"><p>Test.</p></div>'
            '</section>'
        )
        # The synthetic span has no data-exceptional flag, so it should fall to Soft
        mi = query.MagicItem(synthetic)
        assert isinstance(mi.attunement, model.Soft)
        assert mi.attunement.value == "Requires Attunement by a Squirrel"

    @staticmethod
    def test_attunement_soft_fallback_on_parse_error(doc):
        """When data-exceptional='logic' is set but data-logic is malformed,
        fall to Soft(text)."""
        from lxml import html as _h
        synthetic = _h.fromstring(
            '<section class="magic-item" id="magic-item-test">'
            '<h4>Test Item</h4>'
            '<p class="magic-item-general">'
            '  <span class="magic-item-category">Wondrous Item</span>, '
            '  <span class="magic-item-rarity">Common</span> '
            '  (<span class="magic-item-attunement" data-exceptional="logic"'
            '         data-logic="garbage(stuff)">Requires Attunement</span>)'
            '</p>'
            '<div class="magic-item-description"><p>Test.</p></div>'
            '</section>'
        )
        mi = query.MagicItem(synthetic)
        assert isinstance(mi.attunement, model.Soft)
        assert mi.attunement.value == "Requires Attunement"

    # ---- Charges: parser unit tests ----

    @staticmethod
    def test_parse_roll_bare(doc):
        assert _parse_roll("1d6") == model.Roll(n=1, d=model.Die.D6, modifier=None)
        assert _parse_roll("2d10") == model.Roll(n=2, d=model.Die.D10, modifier=None)

    @staticmethod
    def test_parse_roll_with_add(doc):
        assert _parse_roll("2d6 + 3") == model.Roll(
            n=2, d=model.Die.D6,
            modifier=model.RollModifier(op=model.Op.ADD, value=3),
        )

    @staticmethod
    def test_parse_roll_with_subtract(doc):
        assert _parse_roll("3d6 - 3") == model.Roll(
            n=3, d=model.Die.D6,
            modifier=model.RollModifier(op=model.Op.SUBTRACT, value=3),
        )

    @staticmethod
    def test_parse_roll_with_multiply(doc):
        assert _parse_roll("1d10 x 10") == model.Roll(
            n=1, d=model.Die.D10,
            modifier=model.RollModifier(op=model.Op.MULTIPLY, value=10),
        )

    @staticmethod
    def test_parse_roll_whitespace_tolerant(doc):
        compact = _parse_roll("2d6+3")
        spaced = _parse_roll("2d6 + 3")
        verbose = _parse_roll("  2d6   +   3  ")
        assert compact == spaced == verbose

    @staticmethod
    def test_parse_roll_rejects_malformed(doc):
        for s in [
            "",                    # empty
            "d6",                  # bare dN (narrative-only in SRD; not structured)
            "1d6 +",               # incomplete modifier
            "1d6 + abc",           # non-numeric modifier value
            "1d6 + 2 + 3",         # chained modifiers (not in SRD grammar)
            "1d5",                 # unknown die size
            "1d6 * 2",             # wrong multiply char (SRD uses 'x')
            "foo",                 # not a roll
        ]:
            try:
                _parse_roll(s)
                raise AssertionError(f"expected parse error for {s!r}")
            except _RollParseError:
                pass

    @staticmethod
    def test_render_roll_roundtrip(doc):
        """Every canonical shape from the survey round-trips through parse -> render."""
        for s in [
            "1d6",
            "2d8",
            "1d20",
            "2d6 + 3",
            "1d8 + 1",
            "3d6 - 3",
            "1d10 x 10",
            "1d4 x 10",
        ]:
            assert _render_roll(_parse_roll(s)) == s, f"roundtrip failed: {s!r}"

    @staticmethod
    def test_parse_charges_dispatch(doc):
        """Presence of 'd' in token selects Rolled; otherwise Fixed."""
        assert _parse_charges("7") == model.Fixed(7)
        assert _parse_charges("50") == model.Fixed(50)
        assert _parse_charges("1d3") == model.Rolled(
            model.Roll(n=1, d=model.Die.D3, modifier=None)
        )
        assert _parse_charges("1d8 + 1") == model.Rolled(
            model.Roll(n=1, d=model.Die.D8,
                       modifier=model.RollModifier(op=model.Op.ADD, value=1))
        )

    # ---- Charges: mapping tests (representative items pinned) ----

    @staticmethod
    def test_wand_of_magic_missiles_charges(doc):
        m = doc.magic_items["wand-of-magic-missiles"]
        assert m.charges == model.Fixed(7)

    @staticmethod
    def test_staff_of_the_magi_charges(doc):
        m = doc.magic_items["staff-of-the-magi"]
        assert m.charges == model.Fixed(50)

    @staticmethod
    def test_luck_blade_charges(doc):
        """Dice capacity (no modifier)."""
        m = doc.magic_items["luck-blade"]
        assert m.charges == model.Rolled(
            model.Roll(n=1, d=model.Die.D3, modifier=None)
        )

    @staticmethod
    def test_nine_lives_stealer_charges(doc):
        """Dice capacity (with add modifier). Span lives inside a non-variant
        special_rule's <dd>, so the parent-level detection must look outside
        the description div."""
        m = doc.magic_items["nine-lives-stealer"]
        assert m.charges == model.Rolled(
            model.Roll(
                n=1, d=model.Die.D8,
                modifier=model.RollModifier(op=model.Op.ADD, value=1),
            )
        )

    @staticmethod
    def test_cube_of_force_charges(doc):
        """Pattern: 'starts with N charges'."""
        m = doc.magic_items["cube-of-force"]
        assert m.charges == model.Fixed(10)

    @staticmethod
    def test_ring_of_three_wishes_charges(doc):
        """Pattern: 'expend 1 of its 3 charges' — wrap the capacity (3), not the spend (1)."""
        m = doc.magic_items["ring-of-three-wishes"]
        assert m.charges == model.Fixed(3)

    @staticmethod
    def test_figurine_ivory_goats_variant_charges(doc):
        """Variant-level charge: figurine itself has no parent-level charges,
        but the Ivory Goats variant carries Fixed(24) via a nested dl/dt/dd."""
        m = doc.magic_items["figurine-of-wondrous-power"]
        assert m.charges is None, f"figurine parent should have no charges, got {m.charges!r}"
        ivory = next((v for v in m.variants if v["name"] == "Ivory Goats"), None)
        assert ivory is not None
        assert ivory["charges"] == model.Fixed(24)

    @staticmethod
    def test_bag_of_holding_no_charges(doc):
        """Items without charge mechanic return None."""
        m = doc.magic_items["bag-of-holding"]
        assert m.charges is None

    # ---- Charges: corpus-wide invariant ----

    @staticmethod
    def test_charges_total_extraction(doc):
        """Sum of parent.charges + variant.charges should match the count of
        <span class='magic-item-charges'> in the source (51 in SRD 5.2.1)."""
        parent_count = sum(1 for m in doc.magic_items if m.charges is not None)
        variant_count = sum(
            1 for m in doc.magic_items
            for v in m.variants if v.get("charges") is not None
        )
        assert parent_count + variant_count == 51, (
            f"expected 51 total charge extractions, "
            f"got {parent_count} parent + {variant_count} variant = "
            f"{parent_count + variant_count}"
        )

    # ---- Charges: to_dict serialization ----

    @staticmethod
    def test_charges_to_dict_shape(doc):
        """to_dict encodes charges as tagged record."""
        d = doc.magic_items["wand-of-magic-missiles"].to_dict()
        assert d["charges"] == {"kind": "fixed", "value": 7}

        d2 = doc.magic_items["luck-blade"].to_dict()
        assert d2["charges"] == {
            "kind": "rolled",
            "value": {"n": 1, "d": "d3", "modifier": None},
        }

        d3 = doc.magic_items["nine-lives-stealer"].to_dict()
        assert d3["charges"] == {
            "kind": "rolled",
            "value": {
                "n": 1, "d": "d8",
                "modifier": {"op": "add", "value": 1},
            },
        }

        # Variant-level
        d4 = doc.magic_items["figurine-of-wondrous-power"].to_dict()
        assert d4["charges"] is None
        ivory = next(v for v in d4["variants"] if v["name"] == "Ivory Goats")
        assert ivory["charges"] == {"kind": "fixed", "value": 24}

        # No charges
        d5 = doc.magic_items["bag-of-holding"].to_dict()
        assert d5["charges"] is None

    @staticmethod
    def test_charges_to_dict_json_serializable(doc):
        """to_dict() output for items with each charge variant must be json-safe."""
        for slug in ["wand-of-magic-missiles", "luck-blade", "nine-lives-stealer",
                     "figurine-of-wondrous-power", "bag-of-holding"]:
            d = doc.magic_items[slug].to_dict()
            json.dumps(d)  # raises if not JSON-serializable


if __name__ == "__main__":
    sys.exit(main())
