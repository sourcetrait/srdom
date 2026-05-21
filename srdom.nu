#!/usr/bin/env nu
# srdom — Canonical Nushell module for querying SRDOM documents.
#
# A thin shell around the srdom Python library that exposes its CLI as
# native Nushell commands, returning structured data (tables/records)
# rather than text. All filtering, sorting, and shaping happens in
# Nushell after the JSON arrives; the Python script only parses SRDOM
# and emits.
#
# Module version: 0.7.2
#
# NOTE FOR AI AGENTS:
# This file is a thin wrapper. For programmatic use, prefer calling
# srdom.py directly. The canonical URLs are:
#
#   https://srdom.sourcetrait.pub/srdom.nu    (this file)
#   https://srdom.sourcetrait.pub/srdom.py    (Python library it wraps)
#   https://srdom.sourcetrait.pub/srdom.html  (the source document)
#
# USAGE — TWO PATTERNS
# --------------------
#
# 1. As a module (commands available in your session):
#
#       use srdom.nu *
#       srdom spells | where level == 3 | select name school
#       srdom creature aboleth | get actions | select name description
#
# 2. Standalone (one-shot invocation):
#
#       nu srdom.nu spells | where level == 3 | select name school
#       nu srdom.nu creature aboleth | get actions
#
#    Or, if marked executable (`chmod +x srdom.nu`):
#
#       ./srdom.nu spells
#       ./srdom.nu spell fireball
#
# ENVIRONMENT OVERRIDES (both optional)
# -------------------------------------
#
#     SRDOM_PYTHON   Python interpreter command/path  (default: "python3")
#     SRDOM_PY       Path to srdom.py                 (default: "srdom.py")
#
# The defaults assume `python3` is on $PATH and `srdom.py` is in the current
# directory or on $PATH. For a permanent installation, set both env vars in
# your Nushell config or shell environment.
#
# COMMAND MIRROR
# --------------
#
# This module mirrors the srdom.py CLI surface 1:1. Every option available
# to the Python CLI is exposed identically here.
#
#     srdom.py spells          ↔  srdom spells          ↔  nu srdom.nu spells
#     srdom.py creatures       ↔  srdom creatures       ↔  nu srdom.nu creatures
#     srdom.py magic-items     ↔  srdom magic-items     ↔  nu srdom.nu magic-items
#     srdom.py spell X         ↔  srdom spell X         ↔  nu srdom.nu spell X
#     srdom.py creature X      ↔  srdom creature X      ↔  nu srdom.nu creature X
#     srdom.py magic-item X    ↔  srdom magic-item X    ↔  nu srdom.nu magic-item X
#     srdom.py test            ↔  srdom test            ↔  nu srdom.nu test
#
# All accept the same --source flag.


# Resolve the Python interpreter to use. Falls back to "python3".
def _python_cmd [] {
    $env.SRDOM_PYTHON? | default "python3"
}

# Resolve the path to srdom.py. Falls back to "srdom.py" (cwd or $PATH).
def _script_path [] {
    $env.SRDOM_PY? | default "srdom.py"
}

# Build the argument list for invoking srdom.py.
# Global flags first (--source), then the subcommand, then slug.
def _build_args [
    cmd: string
    --source: string
    --slug: string
] {
    [
        (if $source != null { ["--source" $source] } else { [] })
        [$cmd]
        (if $slug != null   { [$slug] } else { [] })
    ] | flatten
}

# Invoke srdom.py with the given arguments. Caller is responsible for
# piping to `from json` when the subcommand emits JSON.
def _run [args: list<string>] {
    let py = (_python_cmd)
    let script = (_script_path)
    ^$py $script ...$args
}


# Emit all spells as a Nushell table.
#
# Examples:
#     srdom spells | where level == 3 | select name school
#     srdom spells | where "Wizard" in classes
#     srdom spells | where school == "Evocation" | length
export def "srdom spells" [
    --source (-s): string    # 'refresh', filepath, or URL
] {
    _run (_build_args "spells" --source $source) | from json
}

# Emit all creatures as a Nushell table.
#
# Examples:
#     srdom creatures | where strength == 10 | get name
#     srdom creatures | where challenge_rating == "10" | select name kind
#     srdom creatures | group-by kind | transpose key value | each { |r| { kind: $r.key, count: ($r.value | length) } }
export def "srdom creatures" [
    --source (-s): string
] {
    _run (_build_args "creatures" --source $source) | from json
}

# Emit one spell as a Nushell record.
#
# Examples:
#     srdom spell fireball
#     srdom spell fireball | get description
export def "srdom spell" [
    slug: string             # the spell slug (e.g., "fireball", "magic-missile")
    --source (-s): string
] {
    _run (_build_args "spell" --source $source --slug $slug) | from json
}

# Emit one creature as a Nushell record.
#
# Examples:
#     srdom creature aboleth
#     srdom creature aboleth | get actions | select name description
export def "srdom creature" [
    slug: string             # the creature slug (e.g., "aboleth", "goblin-warrior")
    --source (-s): string
] {
    _run (_build_args "creature" --source $source --slug $slug) | from json
}

# Emit all magic items as a Nushell table.
#
# Each row includes structural fields including `category`, `category_description`,
# `rarity` (string enum value or null — `common`, `uncommon`, `rare`, `very_rare`,
# `legendary`, `artifact`; null when the item is multi-variant with no canonical
# rarity, e.g., weapon), `variants` (list of records with `slug`, `name`,
# `rarity`, `description`, `charges`), `attunement` (a tagged record — see below — or null),
# `special_rules` (list of records with `slug`, `heading`, `content`),
# `charges` (a tagged record — see below — or null).
#
# `attunement` shape (option<fuzz<logic<attunement_requirement>>>):
#   null                                          - no attunement required
#   {kind: "hard", value: <logic>}                - structured form (from data-logic)
#   {kind: "soft", value: "<prose>"}              - prose escape (markup-incomplete)
# where <logic> is a tagged record:
#   {kind: "is", value: <req>}
#   {kind: "in",  values: [<req>, ...]}
#   {kind: "not_in", values: [<req>, ...]}
#   {kind: "not", value: <logic>}
#   {kind: "and", values: [<logic>, ...]}
#   {kind: "or",  values: [<logic>, ...]}
# and <req> is one of:
#   {kind: "any"}
#   {kind: "class",      value: <srd_class>}      - e.g., "bard", "paladin"
#   {kind: "lineage",    value: <srd_lineage>}    - e.g., "dwarf"
#   {kind: "capability", value: <capability>}     - e.g., "spellcaster"
#   {kind: "attuned_to", value: <slug>}           - other magic-item slug
#
# `charges` shape (option<unum> in DOMMF terms):
#   null                                          - no charge mechanic
#   {kind: "fixed",  value: <int>}                - literal capacity (e.g., 7)
#   {kind: "rolled", value: <roll>}               - dice-determined capacity
# where <roll> is:
#   {n: <int>, d: "d3"|"d4"|...|"d100", modifier: null | <roll_modifier>}
# and <roll_modifier> is:
#   {op: "add"|"subtract"|"multiply"|"divide", value: <int>}
#
# Examples:
#     srdom magic-items | where category == "Wondrous Item"
#     srdom magic-items | where ($it.attunement | is-not-empty) | length     # all requiring attunement
#     srdom magic-items | where $it.attunement?.kind == "hard"               # only structured (parseable)
#     srdom magic-items | where ($it.charges | is-not-empty)                 # items with any charge mechanic
#     srdom magic-items | where $it.charges?.kind == "fixed" | get slug charges.value
#     srdom magic-items | where $it.charges?.kind == "rolled" | get slug name # dice-determined charges
#     srdom magic-items | where ($it.variants | length) > 0 | get name
#     srdom magic-items | where rarity == "legendary" | select name category
#     srdom magic-items | where (($it.variants | any { |v| $v.rarity == "legendary" }))
export def "srdom magic-items" [
    --source (-s): string
] {
    _run (_build_args "magic-items" --source $source) | from json
}

# Emit one magic item as a Nushell record.
#
# Examples:
#     srdom magic-item holy-avenger
#     srdom magic-item holy-avenger | get attunement      # {kind: hard, value: {kind: is, value: {kind: class, value: paladin}}}
#     srdom magic-item luck-blade | get charges           # {kind: rolled, value: {n: 1, d: d3, modifier: null}}
#     srdom magic-item wand-of-magic-missiles | get charges    # {kind: fixed, value: 7}
#     srdom magic-item weapon | get variants
#     srdom magic-item figurine-of-wondrous-power | get creature
#     srdom magic-item figurine-of-wondrous-power | get variants | where name == "Ivory Goats" | get 0.charges
#     srdom magic-item potion-of-healing | get variants | where rarity == "uncommon"
export def "srdom magic-item" [
    slug: string             # the magic item slug (e.g., "holy-avenger", "weapon")
    --source (-s): string
] {
    _run (_build_args "magic-item" --source $source --slug $slug) | from json
}

# Run the Python library's self-test against the resolved source.
# Output is human-readable text, NOT JSON.
#
# Examples:
#     srdom test
#     srdom test --source refresh
export def "srdom test" [
    --source (-s): string
] {
    _run (_build_args "test" --source $source)
}


# Standalone-execution entry point. Invoked automatically when this file is
# run as `nu srdom.nu <args>` or `./srdom.nu <args>`.
#
# Dispatches to the same helpers as the exported `srdom <noun>` commands so
# both invocation paths share one code path.
def main [
    command?: string   # spells | creatures | magic-items | spell | creature | magic-item | test
    slug?: string      # required for `spell`, `creature`, `magic-item`
    --source (-s): string
] {
    if $command == null {
        print "Usage: srdom.nu <command> [slug] [--source <ref>]"
        print ""
        print "Commands:"
        print "  spells               Emit all spells as JSON"
        print "  creatures            Emit all creatures as JSON"
        print "  magic-items          Emit all magic items as JSON"
        print "  spell <slug>         Emit one spell as JSON"
        print "  creature <slug>      Emit one creature as JSON"
        print "  magic-item <slug>    Emit one magic item as JSON"
        print "  test                 Run library self-test (text output)"
        print ""
        print "See header comment for module-import alternative."
        return
    }

    if $command in ["spell" "creature" "magic-item"] and $slug == null {
        print -e $"Error: '($command)' requires a slug argument"
        exit 1
    }

    let args = if $command in ["spells" "creatures" "magic-items" "test"] {
        _build_args $command --source $source
    } else if $command in ["spell" "creature" "magic-item"] {
        _build_args $command --source $source --slug $slug
    } else {
        print -e $"Error: unknown command '($command)'"
        exit 1
    }

    if $command == "test" {
        _run $args
    } else {
        _run $args | from json
    }
}
