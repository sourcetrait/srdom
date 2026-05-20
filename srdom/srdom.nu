#!/usr/bin/env nu
# srdom — Canonical Nushell module for querying SRDOM documents.
#
# A thin shell around the srdom Python library that exposes its CLI as
# native Nushell commands, returning structured data (tables/records)
# rather than text. All filtering, sorting, and shaping happens in
# Nushell after the JSON arrives; the Python script only parses SRDOM
# and emits.
#
# Module version: 0.2.0-draft
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
# All accept the same --source and --content flags.


# Resolve the Python interpreter to use. Falls back to "python3".
def _python_cmd [] {
    $env.SRDOM_PYTHON? | default "python3"
}

# Resolve the path to srdom.py. Falls back to "srdom.py" (cwd or $PATH).
def _script_path [] {
    $env.SRDOM_PY? | default "srdom.py"
}

# Build the argument list for invoking srdom.py.
# Global flags first (--source, --content), then the subcommand, then slug.
def _build_args [
    cmd: string
    --content: string
    --source: string
    --slug: string
] {
    [
        (if $source != null  { ["--source"  $source]  } else { [] })
        (if $content != null { ["--content" $content] } else { [] })
        [$cmd]
        (if $slug != null    { [$slug] } else { [] })
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
    --content (-c): string   # "md" (default in srdom.py) or "html"
    --source (-s): string    # 'refresh', filepath, or URL
] {
    _run (_build_args "spells" --content $content --source $source) | from json
}

# Emit all creatures as a Nushell table.
#
# Each row has a `category` field with one of these values:
#   "monster"               — listed under "Monsters A-Z"
#   "animal"                — listed under "Animals"
#   "embedded-spell"        — stat block embedded inside a spell description
#   "embedded-magic-item"   — stat block embedded inside a magic-item description
#
# Examples:
#     srdom creatures | where category == "monster" and strength == 10 | get name
#     srdom creatures | where cr_numeric >= 15 | select name cr
#     srdom creatures | group-by type | transpose key value | each { |r| { type: $r.key, count: ($r.value | length) } }
export def "srdom creatures" [
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "creatures" --content $content --source $source) | from json
}

# Emit one spell as a Nushell record.
#
# Examples:
#     srdom spell fireball
#     srdom spell fireball | get description
export def "srdom spell" [
    slug: string             # the spell slug (e.g., "fireball", "magic-missile")
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "spell" --content $content --source $source --slug $slug) | from json
}

# Emit one creature as a Nushell record.
#
# Examples:
#     srdom creature aboleth
#     srdom creature aboleth | get actions | select name description
export def "srdom creature" [
    slug: string             # the creature slug (e.g., "aboleth", "goblin-warrior")
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "creature" --content $content --source $source --slug $slug) | from json
}

# Emit all magic items as a Nushell table.
#
# Each row includes structural fields including `category`, `category_description`,
# `rarities` (list), `rarity_tiers` (paired list), `requires_attunement` (bool),
# `attunement` (full clause or null), `variants` (list), `num_variants` (int).
#
# Examples:
#     srdom magic-items | where category == "Wondrous Item"
#     srdom magic-items | where requires_attunement == true | length
#     srdom magic-items | where num_variants > 0 | get title
#     srdom magic-items | where ("Legendary" in rarities) | select name category
export def "srdom magic-items" [
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "magic-items" --content $content --source $source) | from json
}

# Emit one magic item as a Nushell record.
#
# Examples:
#     srdom magic-item holy-avenger
#     srdom magic-item weapon | get rarity_tiers
#     srdom magic-item figurine-of-wondrous-power | get creature
export def "srdom magic-item" [
    slug: string             # the magic item slug (e.g., "holy-avenger", "weapon")
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "magic-item" --content $content --source $source --slug $slug) | from json
}

# Run the Python library's self-test against the resolved source.
# Output is human-readable text, NOT JSON.
#
# Examples:
#     srdom test
#     srdom test --source refresh
export def "srdom test" [
    --content (-c): string
    --source (-s): string
] {
    _run (_build_args "test" --content $content --source $source)
}


# Standalone-execution entry point. Invoked automatically when this file is
# run as `nu srdom.nu <args>` or `./srdom.nu <args>`.
#
# Dispatches to the same helpers as the exported `srdom <noun>` commands so
# both invocation paths share one code path.
def main [
    command?: string   # spells | creatures | magic-items | spell | creature | magic-item | test
    slug?: string      # required for `spell`, `creature`, `magic-item`
    --content (-c): string
    --source (-s): string
] {
    if $command == null {
        print "Usage: srdom.nu <command> [slug] [--content md|html] [--source <ref>]"
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
        _build_args $command --content $content --source $source
    } else if $command in ["spell" "creature" "magic-item"] {
        _build_args $command --content $content --source $source --slug $slug
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
