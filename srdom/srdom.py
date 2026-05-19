"""
srdom — Canonical Python library for querying SRDOM documents.

SRDOM is a deliberately structured HTML representation of the D&D SRD 5.2.1.
This library provides typed, indexed access to its spells and creatures,
backed by lxml for fast XPath queries against the underlying DOM.

The library is designed primarily for programmatic consumers — including AI
agents — that need to extract structured data from the SRD without crafting
regex patterns. It encodes the document's structural conventions so callers
don't have to learn them.

QUICK START
-----------

    >>> import srdom
    >>> doc = srdom.load("srdom.html")
    >>> len(doc.spells)
    339
    >>> len(doc.creatures)
    336
    >>> spell = doc.spells["fireball"]
    >>> spell.name
    'Fireball'
    >>> spell.level
    3
    >>> spell.casting_time
    'Action'
    >>> spell.components
    Components(verbal=True, somatic=True, material='a ball of bat guano and sulfur')

DEPENDENCIES
------------

Requires `lxml` (https://lxml.de/). No other third-party dependencies. lxml is
included in most scientific Python distributions; install via
`pip install lxml` if not already available.

CONVENTIONS
-----------

- All accessors are read-only. Mutation is not supported in this version.
- Field access uses Python-idiomatic snake_case names (`casting_time`,
  `legendary_actions`) regardless of the underlying HTML class names.
- Optional fields return None when absent (e.g., a cantrip's `.level` is None,
  a creature without resistances has `.resistances` == None).
- List fields return empty lists when absent (e.g., a spell with no effects
  has `.effects` == []).
- Each entity exposes `.element` as an escape hatch to the underlying lxml
  element for ad-hoc XPath queries.

DOM CONTRACT
------------

This library targets the SRDOM structural conventions:

- Spells: <section class="spell" id="spell-{slug}">
- Creatures: <section class="creature" id="creature-{slug}">
- Field values carry single prefixed classes: spell-cast-time, creature-ac, etc.
- Element types vary by content (td for cells, span for inline metadata,
  dt/dd for named lists, h-tags for headings).

For the full DOM contract with worked examples, see the Usage section at the
end of srdom.html (anchor #using-srdom).

CANONICAL URL
-------------

This library is published at: https://srdom.sourcetrait.pub/srdom.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Union

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


__version__ = "0.1.0"
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
]


# ============================================================================
# Module-level entry point
# ============================================================================


def load(path: str) -> "Document":
    """Parse an SRDOM document and return a Document object.

    Args:
        path: Filesystem path to the srdom.html file.

    Returns:
        A Document instance with indexed access to spells and creatures.

    Example:
        >>> doc = srdom.load("srdom.html")
        >>> len(doc.spells)
        339
    """
    with open(path, "rb") as f:
        tree = _lhtml.fromstring(f.read())
    return Document(tree)


# ============================================================================
# Document
# ============================================================================


class Document:
    """A parsed SRDOM document.

    Provides indexed access to the document's spells and creatures via the
    `.spells` and `.creatures` collection accessors.

    Attributes:
        spells: SpellCollection (indexable by slug, iterable, filterable).
        creatures: CreatureCollection (same interface as spells).
        version: The document's declared version string (e.g., "0.4.0").

    Example:
        >>> doc = srdom.load("srdom.html")
        >>> doc.spells["fireball"]
        <Spell 'fireball': Level 3 Evocation>
        >>> doc.creatures["aboleth"]
        <Creature 'aboleth': Large Aberration, CR 10>
    """

    def __init__(self, tree: _Element):
        self._tree = tree
        self._spells = SpellCollection(tree)
        self._creatures = CreatureCollection(tree)

    @property
    def spells(self) -> "SpellCollection":
        """All spells in the document, indexable by slug.

        Example:
            >>> doc.spells["fireball"].casting_time
            'Action'
        """
        return self._spells

    @property
    def creatures(self) -> "CreatureCollection":
        """All creatures in the document, indexable by slug.

        Example:
            >>> doc.creatures["aboleth"].ac
            '17'
        """
        return self._creatures

    @property
    def version(self) -> Optional[str]:
        """The document's declared version string from <meta name="version">.

        Example:
            >>> doc.version
            '0.4.0'
        """
        result = self._tree.xpath('//meta[@name="version"]/@content')
        return result[0] if result else None

    @property
    def element(self) -> _Element:
        """The underlying lxml root element for ad-hoc queries.

        Example:
            >>> all_links = doc.element.xpath('//a/@href')
        """
        return self._tree

    def __repr__(self) -> str:
        return (
            f"<Document version={self.version!r} "
            f"spells={len(self._spells)} creatures={len(self._creatures)}>"
        )


# ============================================================================
# Collections
# ============================================================================


class _BaseCollection:
    """Common implementation for SpellCollection and CreatureCollection."""

    _entity_class = None  # overridden by subclasses
    _id_prefix = None  # e.g., "spell-" or "creature-"
    _section_class = None  # e.g., "spell" or "creature"

    def __init__(self, tree: _Element):
        self._tree = tree

    def __getitem__(self, slug: str):
        """Get an entity by its slug (e.g., 'fireball', 'aboleth').

        Raises KeyError if the slug is not found.
        """
        # id() is the fastest XPath form for unique-ID lookup (~12 μs vs ~18 ms
        # for //*[@id=...] descent). See benchmark in audit-pass-9.
        result = self._tree.xpath(f'id("{self._id_prefix}{slug}")')
        if not result:
            raise KeyError(slug)
        return self._entity_class(result[0])

    def __iter__(self) -> Iterator:
        """Iterate over all entities in document order."""
        for el in self._tree.xpath(f'//section[@class="{self._section_class}"]'):
            yield self._entity_class(el)

    def __len__(self) -> int:
        return len(self._tree.xpath(f'//section[@class="{self._section_class}"]'))

    def __contains__(self, slug: str) -> bool:
        return bool(self._tree.xpath(f'id("{self._id_prefix}{slug}")'))

    def filter(self, **kwargs) -> Iterator:
        """Filter entities by field values.

        Keyword args support exact match by default. Suffixes provide more
        operators:
        - field=value       : exact match (==)
        - field__ne=value   : not equal
        - field__lt=value   : less than
        - field__lte=value  : less than or equal
        - field__gt=value   : greater than
        - field__gte=value  : greater than or equal
        - field__in=iter    : membership in iterable
        - field__contains=str : substring (for list/string fields)

        Example:
            >>> list(doc.spells.filter(level=3, school="Evocation"))
            [<Spell 'fireball': Level 3 Evocation>, ...]
            >>> list(doc.creatures.filter(cr__gte=10))
            [<Creature 'aboleth': ...>, ...]
        """
        for entity in self:
            if all(_match(entity, k, v) for k, v in kwargs.items()):
                yield entity


def _match(entity, key: str, expected) -> bool:
    """Apply a filter predicate to an entity field."""
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
    """All 339 spells in the document.

    Example:
        >>> len(doc.spells)
        339
        >>> "fireball" in doc.spells
        True
        >>> doc.spells["fireball"].school
        'Evocation'
        >>> [s.name for s in doc.spells.filter(level=9)][:3]
        ['Astral Projection', 'Foresight', 'Gate']
    """
    _section_class = "spell"
    _id_prefix = "spell-"

    def __init__(self, tree):
        super().__init__(tree)
        # _entity_class set after Spell is defined; see end of file


class CreatureCollection(_BaseCollection):
    """All 336 creature stat blocks in the document.

    Example:
        >>> len(doc.creatures)
        336
        >>> "aboleth" in doc.creatures
        True
        >>> doc.creatures["aboleth"].type
        'Aberration'
    """
    _section_class = "creature"
    _id_prefix = "creature-"

    def __init__(self, tree):
        super().__init__(tree)


# ============================================================================
# Helpers and value types
# ============================================================================


@dataclass(frozen=True)
class Components:
    """Parsed spell components.

    Attributes:
        verbal: True if the spell requires verbal components.
        somatic: True if the spell requires somatic components.
        material: The material description string, or None if the spell does
                  not require material components.

    Example:
        >>> doc.spells["fireball"].components
        Components(verbal=True, somatic=True, material='a ball of bat guano and sulfur')
        >>> doc.spells["fireball"].components.material
        'a ball of bat guano and sulfur'
        >>> doc.spells["fireball"].components.verbal
        True
    """
    verbal: bool
    somatic: bool
    material: Optional[str]

    @classmethod
    def parse(cls, raw: str) -> "Components":
        """Parse a raw components string like 'Verbal, Somatic, Material (foo)'."""
        verbal = "Verbal" in raw
        somatic = "Somatic" in raw
        material = None
        if "Material" in raw:
            m = re.search(r"Material\s*\(([^)]+)\)", raw)
            material = m.group(1) if m else ""
        return cls(verbal=verbal, somatic=somatic, material=material)


@dataclass(frozen=True)
class CR:
    """Challenge Rating, supporting both fractional and integer values.

    CR comparison works as expected: CR("1/4") < CR("1/2") < CR(1) < CR(10).
    Equality is also numeric (CR("2/4") == CR("1/2")).

    Attributes:
        numerator: The integer numerator.
        denominator: The integer denominator (1 for whole numbers).

    Example:
        >>> creature = doc.creatures["aboleth"]
        >>> creature.cr
        CR(10)
        >>> creature.cr.numerator
        10
        >>> str(creature.cr)
        '10'
        >>> doc.creatures["goblin-warrior"].cr < doc.creatures["aboleth"].cr
        True
    """
    numerator: int
    denominator: int = 1

    @classmethod
    def parse(cls, raw: str) -> "CR":
        """Parse a CR string like '10', '1/4', '1/8'."""
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
        return f"CR({str(self)})"

    @property
    def numeric(self) -> float:
        """Numeric value for arithmetic (e.g., CR('1/4').numeric == 0.25)."""
        return self.numerator / self.denominator

    # Comparison is numeric, not lexicographic on fields.
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
    """Common shape for named items: spell effects, creature traits, actions,
    etc.

    Attributes:
        name: The item's display name.
        slug: The item's slug suffix (e.g., 'using-a-higher-level-spell-slot').
        description: The item's body content as plain text. Multiple paragraphs
                     are joined with double newlines. List items are joined
                     with newlines.
        description_html: The item's body as raw HTML.
        constraint: Inline parenthetical constraint (e.g., 'Recharge 5-6'),
                    None if absent. Only relevant for creature items; always
                    None for spell effects.
        element: The underlying lxml <dd> element.
    """

    def __init__(self, dt: _Element, dd: _Element):
        self._dt = dt
        self._dd = dd

    @property
    def name(self) -> str:
        # The dt may contain a <span class="constraint"> inline; the name is
        # the dt's text minus that span.
        # However, per current SRDOM structure, the constraint span lives
        # inside the dd's first <p>, not in the dt. The dt is plain text.
        return self._dt.text_content().strip()

    @property
    def slug(self) -> str:
        """The item's slug from its ID."""
        full_id = self._dd.get("id", "")
        # Pattern: "{prefix}-effect-{slug}" or "{prefix}-trait-{slug}", etc.
        # Take whatever follows the last occurrence of the item-type segment.
        parts = full_id.split("-")
        # Find the item-type marker (effect, trait, action, etc.) and return
        # everything after it.
        for marker in ("effect", "trait", "action", "reaction"):
            if marker in parts:
                idx = parts.index(marker)
                return "-".join(parts[idx + 1:])
        return full_id

    @property
    def description(self) -> str:
        """The body content as plain text, with paragraph breaks preserved."""
        parts = []
        for child in self._dd:
            if child.tag == "p":
                parts.append(child.text_content().strip())
            elif child.tag in ("ul", "ol"):
                items = [
                    f"- {li.text_content().strip()}" for li in child.findall("li")
                ]
                parts.append("\n".join(items))
            elif child.tag == "table":
                # Best-effort plaintext for tables; rare in items.
                parts.append(child.text_content().strip())
        return "\n\n".join(p for p in parts if p)

    @property
    def description_html(self) -> str:
        """The body content as raw HTML."""
        return "".join(
            _lhtml.tostring(child, encoding="unicode") for child in self._dd
        )

    @property
    def constraint(self) -> Optional[str]:
        """Inline parenthetical constraint, e.g., 'Recharge 5-6'. None if absent."""
        result = self._dd.xpath('.//span[@class="constraint"]/text()')
        return result[0] if result else None

    @property
    def element(self) -> _Element:
        return self._dd

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


class Effect(_NamedItem):
    """A named effect on a spell or creature trait (e.g., "Using a Higher-Level
    Spell Slot", "Familiarity")."""
    pass


class Trait(_NamedItem):
    """A named creature trait (e.g., "Amphibious", "Spellcasting")."""
    pass


class Action(_NamedItem):
    """A named creature action, bonus action, reaction, or legendary action."""
    pass


@dataclass(frozen=True)
class Special:
    """An embedded reference sub-section in a spell (e.g., Control Weather's
    Precipitation table).

    Attributes:
        heading: The sub-section's heading text.
        slug: The slug suffix from the section's ID.
        html: The full HTML of the sub-section, including its <table>.
        table_html: The inner table's HTML only.

    Example:
        >>> spell = doc.spells["control-weather"]
        >>> [s.heading for s in spell.specials]
        ['Precipitation', 'Temperature', 'Wind']
    """
    heading: str
    slug: str
    html: str
    table_html: str


# ============================================================================
# Spell
# ============================================================================


class Spell:
    """A spell entry in SRDOM.

    Provides typed accessors for metadata, casting fields, description,
    effects, embedded reference tables (specials), and embedded creatures
    (for summoning spells).

    Attributes:
        slug: The spell's slug (e.g., 'fireball').
        name: The spell's display name.
        level: Spell level as int (1-9), or None for cantrips.
        upgrade: "Cantrip" for cantrips, None for leveled spells.
        school: The spell's school (e.g., 'Evocation').
        classes: List of class names that can cast this spell.
        classes_raw: The raw comma-separated classes string.
        casting_time: Casting time as a plain string (e.g., 'Action').
        range: Spell range (e.g., '60 feet').
        components: Components dataclass with verbal/somatic/material fields.
        components_raw: The raw components string (e.g., 'Verbal, Somatic').
        duration: Duration string (e.g., 'Instantaneous').
        description: Description prose as plain text.
        description_html: Description prose as raw HTML.
        effects: List of Effect objects (empty if none).
        specials: List of Special objects for embedded reference tables.
        creature: Embedded Creature for summoning spells, None otherwise.
        element: Underlying lxml element.

    Example:
        >>> spell = doc.spells["fireball"]
        >>> spell.name
        'Fireball'
        >>> spell.level
        3
        >>> spell.school
        'Evocation'
        >>> spell.classes
        ['Sorcerer', 'Wizard']
        >>> spell.casting_time
        'Action'
        >>> spell.components.material
        'a ball of bat guano and sulfur'
        >>> spell.effects[0].name
        'Using a Higher-Level Spell Slot'
    """

    def __init__(self, element: _Element):
        self._element = element

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
        """Returns list of class names (e.g., ['Sorcerer', 'Wizard'])."""
        return [c.strip() for c in self.classes_raw.split(",")]

    @property
    def classes_raw(self) -> str:
        """Returns the raw class list string (e.g., 'Sorcerer, Wizard')."""
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
        """Parsed components as a Components dataclass."""
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
        """Description prose as plain text, with paragraph breaks preserved."""
        descs = self._element.xpath('./div[@class="spell-description"]')
        if not descs:
            return ""
        paragraphs = [p.text_content().strip() for p in descs[0].xpath("./p")]
        return "\n\n".join(p for p in paragraphs if p)

    @property
    def description_html(self) -> str:
        """Description as raw HTML."""
        descs = self._element.xpath('./div[@class="spell-description"]')
        if not descs:
            return ""
        return "".join(
            _lhtml.tostring(child, encoding="unicode") for child in descs[0]
        )

    @property
    def effects(self) -> List[Effect]:
        """List of named effects (empty list if none)."""
        dl = self._element.xpath('./dl[@class="spell-effects"]')
        if not dl:
            return []
        dl = dl[0]
        dts = dl.xpath("./dt")
        dds = dl.xpath("./dd")
        return [Effect(dt, dd) for dt, dd in zip(dts, dds)]

    @property
    def specials(self) -> List[Special]:
        """List of embedded reference sub-sections (empty list if none).

        Used by spells with embedded tables, e.g., Control Weather has
        Precipitation, Temperature, Wind; Teleport has Teleportation Outcome.
        """
        result = []
        for sec in self._element.xpath(
            './div[@class="spell-specials"]/section[@class="spell-special"]'
        ):
            heading_text = sec.xpath("./h5/text() | ./h6/text()")
            heading = heading_text[0] if heading_text else ""
            full_id = sec.get("id", "")
            # ID pattern: "spell-{spell-slug}-special-{slug}"
            parts = full_id.split("-special-")
            slug = parts[1] if len(parts) == 2 else full_id
            tables = sec.xpath("./table")
            table_html = _lhtml.tostring(tables[0], encoding="unicode") if tables else ""
            html = _lhtml.tostring(sec, encoding="unicode")
            result.append(Special(
                heading=heading, slug=slug, html=html, table_html=table_html
            ))
        return result

    @property
    def creature(self) -> Optional["Creature"]:
        """Embedded creature for summoning spells (Animate Objects, Find Steed,
        Giant Insect, Summon Dragon); None for all other spells.
        """
        result = self._element.xpath('./section[@class="creature"]')
        return Creature(result[0]) if result else None

    def __repr__(self) -> str:
        if self.level is not None:
            return f"<Spell {self.slug!r}: Level {self.level} {self.school}>"
        return f"<Spell {self.slug!r}: {self.school} Cantrip>"


# ============================================================================
# Creature
# ============================================================================


class Creature:
    """A creature stat block in SRDOM.

    Provides typed accessors for the creature's metadata, combat highlights,
    ability scores, details, and named stat-block subsections (traits, actions,
    bonus actions, reactions, legendary actions).

    Attributes:
        slug, name: Identification.
        size, type, alignment: Type-line metadata.
        ac, hp, speed, initiative: Combat highlights as raw strings.
        strength, dexterity, constitution, intelligence, wisdom, charisma:
            Ability scores as ints.
        strength_modifier, ... charisma_modifier: Ability modifiers as strings
            (preserving the "+" or "−" sign).
        strength_save, ... charisma_save: Saving throw modifiers as strings.
        senses, languages, cr, skills, immunities, resistances, gear,
        vulnerabilities: Details as raw strings; None if absent.
        traits, actions, bonus_actions, reactions, legendary_actions:
            Lists of named items.
        element: Underlying lxml element.

    Example:
        >>> creature = doc.creatures["aboleth"]
        >>> creature.name
        'Aboleth'
        >>> creature.size
        'Large'
        >>> creature.type
        'Aberration'
        >>> creature.alignment
        'Lawful Evil'
        >>> creature.ac
        '17'
        >>> creature.strength
        21
        >>> creature.strength_modifier
        '+5'
        >>> creature.cr
        CR(10)
    """

    _ABILITIES = (
        "strength", "dexterity", "constitution",
        "intelligence", "wisdom", "charisma",
    )

    _OPTIONAL_DETAILS = (
        "skills", "immunities", "resistances", "gear", "vulnerabilities"
    )

    def __init__(self, element: _Element):
        self._element = element

    @property
    def element(self) -> _Element:
        return self._element

    @property
    def slug(self) -> str:
        return self._element.get("id", "").removeprefix("creature-")

    @property
    def name(self) -> str:
        # Creature name is in h3 (top-level) or h5 (embedded in spells).
        result = self._element.xpath('./h3[@class="creature-name"]/text()')
        if not result:
            result = self._element.xpath('./h5[@class="creature-name"]/text()')
        return result[0] if result else ""

    # ---- Type-line metadata --------------------------------------------

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

    # ---- Abilities ------------------------------------------------------

    def __getattr__(self, name: str):
        """Dynamic dispatch for ability scores and saves.

        Handles attributes of the form:
        - <ability>:           int  (e.g., creature.strength → 21)
        - <ability>_modifier:  str  (e.g., creature.strength_modifier → '+5')
        - <ability>_save:      str  (e.g., creature.strength_save → '+9')
        """
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
        # Optional details (return None if absent)
        if name in self._OPTIONAL_DETAILS:
            return self._optional_text(f'.//td[@class="creature-{name}"]/text()')
        raise AttributeError(
            f"{type(self).__name__!r} has no attribute {name!r}"
        )

    # ---- Details ---------------------------------------------------------

    @property
    def senses(self) -> str:
        return self._first_text('.//td[@class="creature-senses"]/text()')

    @property
    def languages(self) -> str:
        return self._first_text('.//td[@class="creature-languages"]/text()')

    @property
    def cr(self) -> Optional[CR]:
        """Challenge Rating, parsed as a CR object (supports comparison).

        Returns None for creatures without a numeric CR — i.e., minions and
        summoned entities whose stat block carries CR "None". Approximately
        5 creatures in SRDOM 5.2.1 have this property.
        """
        raw = self._first_text('.//td[@class="creature-cr"]/text()')
        # CR cells often carry a " (XP N)" suffix — strip it.
        raw = raw.split("(")[0].strip()
        if not raw or raw.lower() == "none":
            return None
        try:
            return CR.parse(raw)
        except ValueError:
            return None

    @property
    def cr_raw(self) -> str:
        """The raw CR cell text, including any XP suffix."""
        return self._first_text('.//td[@class="creature-cr"]/text()')

    # skills, immunities, resistances, gear, vulnerabilities handled via __getattr__

    # ---- Stat-block subsections ----------------------------------------

    @property
    def traits(self) -> List[Trait]:
        """List of creature traits (empty if none)."""
        return self._named_items("creature-traits", Trait)

    @property
    def actions(self) -> List[Action]:
        """List of creature actions (empty if none)."""
        return self._named_items("creature-actions", Action)

    @property
    def bonus_actions(self) -> List[Action]:
        """List of creature bonus actions (empty if none)."""
        return self._named_items("creature-bonus-actions", Action)

    @property
    def reactions(self) -> List[Action]:
        """List of creature reactions (empty if none)."""
        return self._named_items("creature-reactions", Action)

    @property
    def legendary_actions(self) -> List[Action]:
        """List of creature legendary actions (empty if none)."""
        return self._named_items("creature-legendary-actions", Action)

    # ---- Internals -----------------------------------------------------

    def _first_text(self, xpath_expr: str) -> str:
        """XPath helper: return the first text node, or '' if absent."""
        result = self._element.xpath(xpath_expr)
        return result[0] if result else ""

    def _optional_text(self, xpath_expr: str) -> Optional[str]:
        """XPath helper: return the first text node, or None if absent."""
        result = self._element.xpath(xpath_expr)
        return result[0] if result else None

    def _named_items(self, section_class: str, item_class) -> List:
        """Get all dt/dd pairs from a named subsection."""
        sections = self._element.xpath(
            f'./section[@class="{section_class}"]/dl'
        )
        if not sections:
            return []
        dl = sections[0]
        dts = dl.xpath("./dt")
        dds = dl.xpath("./dd")
        return [item_class(dt, dd) for dt, dd in zip(dts, dds)]

    def __repr__(self) -> str:
        try:
            cr_str = f"CR {self.cr}" if self.cr is not None else "CR none"
            return (
                f"<Creature {self.slug!r}: {self.size} {self.type}, {cr_str}>"
            )
        except (IndexError, ValueError):
            return f"<Creature {self.slug!r}>"


# Resolve forward references
SpellCollection._entity_class = Spell
CreatureCollection._entity_class = Creature


# ============================================================================
# Self-test (executed when the module is run directly)
# ============================================================================


def _self_test(path: str = "srdom.html") -> None:
    """Run sanity checks against an SRDOM document.

    Prints a summary of counts and spot-checks for several entities. Used as
    a smoke test after lib edits.

    Example:
        $ python srdom.py srdom.html
    """
    print(f"Loading {path}...")
    doc = load(path)
    print(f"  Document version: {doc.version}")
    print(f"  Spell count: {len(doc.spells)}")
    print(f"  Creature count: {len(doc.creatures)}")

    print("\n--- Spell spot checks ---")
    for slug in ["acid-arrow", "fireball", "magic-missile", "wish", "acid-splash"]:
        s = doc.spells[slug]
        print(f"  {s}: casting_time={s.casting_time!r}, "
              f"components={s.components}")

    print("\n--- Creature spot checks ---")
    for slug in ["aboleth", "ancient-red-dragon", "goblin-warrior", "commoner"]:
        try:
            c = doc.creatures[slug]
            print(f"  {c}: AC={c.ac}, HP={c.hp}, STR={c.strength}")
        except KeyError:
            print(f"  (no creature: {slug})")

    print("\n--- Effect spot check ---")
    fireball = doc.spells["fireball"]
    for eff in fireball.effects:
        print(f"  Fireball effect: {eff.name!r}")
        print(f"    description: {eff.description[:80]}...")

    print("\n--- Specials spot check ---")
    cw = doc.spells["control-weather"]
    print(f"  Control Weather specials: {[s.heading for s in cw.specials]}")

    print("\n--- Embedded creature spot check ---")
    fs = doc.spells["find-steed"]
    if fs.creature:
        print(f"  Find Steed embedded creature: {fs.creature}")

    print("\n--- Traits/actions spot check ---")
    aboleth = doc.creatures["aboleth"]
    print(f"  Aboleth traits ({len(aboleth.traits)}): "
          f"{[t.name for t in aboleth.traits[:3]]}")
    print(f"  Aboleth actions ({len(aboleth.actions)}): "
          f"{[a.name for a in aboleth.actions[:3]]}")
    print(f"  Aboleth legendary_actions ({len(aboleth.legendary_actions)}): "
          f"{[a.name for a in aboleth.legendary_actions[:3]]}")

    print("\n--- Filter spot check ---")
    cantrips = list(doc.spells.filter(level=None))
    print(f"  Cantrips: {len(cantrips)}")
    level_9 = list(doc.spells.filter(level=9))
    print(f"  Level 9 spells: {len(level_9)}")
    high_cr = list(doc.creatures.filter(cr__gte=CR(15)))
    print(f"  Creatures with CR >= 15: {len(high_cr)}")

    print("\n--- CR comparison ---")
    print(f"  CR('1/4') < CR(1): {CR.parse('1/4') < CR.parse('1')}")
    print(f"  Aboleth CR: {aboleth.cr} ({aboleth.cr.numeric})")

    print("\nAll spot checks completed.")


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "srdom.html"
    _self_test(path)
