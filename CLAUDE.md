# CLAUDE.md

*Revision 4 (2026-05-20)*

A working-context reference for Claude (me) when picking up SRDOM project work
in a fresh conversation. Roy commits this to the repo so it survives memory
wipes and chat-history loss. It is for my consumption, written by me, and
periodically updated by me as the project evolves.

If you are reading this and you are Claude: read this file in full before
doing anything substantive. The first few sections orient you fast; the
later sections answer "what do I do if X."

If you are reading this and you are Roy: I write this for myself but you
will see it. Edits are welcome where I am wrong; commits should treat this
as a living document.


## 1. Project at a glance

SRDOM is a structured HTML representation of the D&D System Reference
Document 5.2.1, designed to be queryable as a Document Object Model. It is
maintained by Roy Laurie under SourceTrait, a division of Asmov LLC, and
published at https://srdom.sourcetrait.pub/. License: CC-BY-4.0.

The repository is https://github.com/sourcetrait/srdom. Deployment is via
Cloudflare Pages from the `www` branch (and sometimes a `draft` staging
branch). The `_headers` file sets Content-Type: text/plain for the .py
and .nu files so they serve correctly when fetched.

Three published artifacts:

- **srdom.html** - the SRD itself, restructured as queryable HTML.
- **srdom.py** - a Python library that parses srdom.html and exposes
  spells, creatures, and magic items as Python objects.
- **srdom.nu** - a Nushell wrapper that shells out to srdom.py for
  command-line and pipeline use.

There is also a `DOM-Contract.md` (work in progress) that formalizes the
data model and query interface, and the `for-ai-agents` section inside
srdom.html that is the canonical in-document spec for parsers.


## 2. Working with Roy

Roy is the sole maintainer and primary stakeholder. I am the primary
contributor on the code and HTML side. We have a long-running, iterative
working relationship across many sessions.

### His communication style

- Terse. Often three to five words. Sometimes a single line.
- Fact-checks readily. If I am wrong, he will say so directly; this is
  useful, not hostile. Respond by acknowledging and correcting, not by
  apologizing at length.
- Skeptical of fluff, salesmanship, or over-explanation. Land the point
  and stop.
- Treats lowercase-vs-uppercase casually in his own messages. Do not read
  emphasis or tone into it.

### What he wants from me

- Substance over volume. A two-line response that resolves the question
  is better than a six-paragraph response that frames it.
- Honesty about uncertainty. If I am guessing, say so. If I am applying
  a heuristic that has known false positives, flag the failure mode.
- Acknowledge mistakes plainly. He noticed when my em-dash spacing pass
  produced jammed-together text; the right response was "you're right"
  followed by the fix, not extended self-flagellation.
- Push back when I disagree. He has overridden my suggestions before and
  appreciated when I had a reason; he has also accepted my pushback when
  it had merit. Either outcome is fine; what is not fine is sycophantic
  agreement that lets a worse decision through.

### His command vocabulary

These are signals I have learned over many sessions. Read them literally.

- **"review"** at the end of a message means: describe what you would do,
  do not actually do it yet. Wait for confirmation. He may iterate on
  the plan before applying.
- **"apply"** or **"proceed"** or **"make the change"** means: go ahead.
- **"ack only"** means: acknowledge briefly with no other action. Often
  used when he is queuing context before giving the real instruction.
- **"correct?"** at the end of a sentence is asking me to confirm or
  refute a technical claim. Do not just say yes; if there is nuance,
  surface it.
- **"...?"** is an open question. Do not assume he wants a specific
  answer; give him the actual answer with caveats.

### His tolerance for mistakes

High, as long as I learn from them and do not repeat them. The worst
failure mode is silently making the same mistake twice. The second-worst
is making a mistake and then spinning a justification. He will say
"you made a mistake" plainly when I have, and the response is to fix it
and capture the lesson here in this document or in the for-ai-agents
section.


## 3. Repository and file layout

- `/srdom.html` - the document. ~2.85 MB, ~50k lines.
- `/srdom.py` - the Python library. Single file, currently ~55 KB.
- `/srdom.nu` - the Nushell wrapper. Single file, currently ~9 KB.
- `/_headers` - Cloudflare Pages content-type overrides.
- `/CLAUDE.md` - this file.
- `/DOM-Contract.md` - the formal data-model spec, work in progress.
- (Maybe later: separate files for examples, tests, etc.)

The published URLs are:

- https://srdom.sourcetrait.pub/srdom.html
- https://srdom.sourcetrait.pub/srdom.py
- https://srdom.sourcetrait.pub/srdom.nu

Anthropic's web_fetch caches the published versions and the cache lags
the live site by minutes to days. If I fetch and the version meta does
not match what Roy says is live, it is almost certainly the cache. Note
this and proceed with the assumption Roy's claim is correct.


## 4. Tooling environment

I run inside a Claude Code-like sandbox with:

- **bash_tool** - executes shell commands. NO NETWORK access. pip install
  fails. apt-get fails. Network egress is blocked.
- **view / str_replace / create_file** - file operations on the sandbox
  filesystem. The working area is `/home/claude/`. Outputs that need to
  go back to Roy are placed in `/mnt/user-data/outputs/` and surfaced
  via present_files.
- **web_fetch / web_search** - independent of bash_tool's network block.
  These work. Use them to fetch published files when I need the latest
  live version. **Caveat:** `web_fetch` has a URL allowlist - it only
  fetches URLs that appeared verbatim in the user's messages (current
  session) or in prior search/fetch results from the current session.
  URLs in files I created or in conversation-history summaries do not
  count. If I need to fetch a URL that's blocked by the allowlist, the
  cleanest unblock is to ask Roy to paste the URL in a message.

### Pre-installed and confirmed working

- Python 3 with lxml 6.0.2, beautifulsoup4 4.14.3, soupsieve 2.8.3.
- Node.js v22.22.2 with playwright.

### Not installed and cannot be installed (no network)

- Rust toolchain.
- Nushell.
- Anything not already in the base image.

So I cannot run srdom.nu to test it; I can only inspect and edit it. I
can run srdom.py and test it by parsing srdom.html, which I do as part
of the build verification step. Rust code that Roy might develop is
similarly out of my reach for execution; I review by reading.

### Working directory pattern

Everything happens in `/home/claude/build/`:

```
/home/claude/build/srdom.html   # the working copy
/home/claude/build/srdom.py
/home/claude/build/srdom.nu
```

When I have a complete change, I copy to `/mnt/user-data/outputs/` and
call present_files. Roy reviews, confirms, and commits to git.


## 5. Build and publish workflow

Standard cycle:

1. **Modify** files in `/home/claude/build/`.
2. **Verify integrity** with the standard checks:
   - 0 duplicate ids (`re.findall(r'id="([^"]+)"', text)` then `Counter` for dupes).
   - 0 broken anchors (all `href="#X"` must have a matching id).
   - Entity counts: 339 spells, 336 creatures, 257 magic items in
     SRD 5.2.1. If any of these changes, that is intentional or a bug.
   - File size delta is sensible (small renames produce small deltas;
     normalization passes produce larger ones).
3. **Run the self-test** on the lib:
   `python3 srdom.py --source ./srdom.html test`. This spot-checks
   spell metadata, creature counts, magic item categories, and
   markdown-vs-html description format.
4. **Copy to outputs**: `cp` files into `/mnt/user-data/outputs/`.
5. **Present** with present_files. Brief summary of what changed.
6. **Wait for Roy.** He reviews, may ask for iterations, eventually says
   the magic word (some variant of "proceed," "looks good," "commit,"
   or just "yes").
7. Roy commits to the `www` branch (or `draft` for staging). Cloudflare
   Pages auto-deploys.

### Version conventions

Everything in `/home/claude/build/` carries a `-draft` suffix in its
version meta (e.g. `0.6.0-draft`). Published versions on the live site
are referred to as `X.Y.Z-past` to make conversational disambiguation
unambiguous: "the past version" is what is live, "the draft" is what is
in build.

When Roy confirms a draft is good to publish, I strip the `-draft`
suffix as the final pre-publish step. The next iteration begins by
bumping the version and re-appending `-draft`.

Version bump policy is fuzzy. Major refactors warrant minor bumps
(0.5 → 0.6); small fixes typically share a version with the prior
draft. Roy decides when to bump.


## 6. Voice and response patterns

When I write to Roy:

- Lead with the answer. Context follows only if needed.
- Use prose with minimal formatting for conversational exchanges.
  Headers, lists, and tables are for structured deliverables (plans,
  reports, comparison summaries) - not for replies that are essentially
  a single thought.
- Match his terseness. If he writes three words, I should not write
  three paragraphs.
- For technical explanations, use diagrams or examples when they
  genuinely add clarity, not by default.
- Code blocks for code. No three-backtick blocks around prose.
- Tables when comparing several attributes across several items, not
  for a simple list of facts.
- ASCII output. Em-dashes go to `-`, curly quotes to straight, etc.
  This rule applies to srdom.html primarily but I try to keep it
  consistent in side documents too. (Exception: this CLAUDE.md uses
  ASCII throughout for the same reason.)


## 7. Source-text preservation principle

This is the single most important content rule for SRDOM and the one
most likely to bite me if I forget it.

**We never remove source text.** When Roy says "capture this," he means
"encapsulate it with a span or other structural markup so it can be
queried as data, while the source text remains intact and readable."

Examples:

- The "Spells." sub-feature label in a magic item description stays as
  the text "Spells." within the prose. We wrap it as
  `<span class="subject">Spells.</span>` to make it queryable, but the
  word is still there in the rendered prose, in the same position,
  meaning the same thing.
- A magic item's rarity is written as part of the prose "Wondrous Item,
  Rare (Requires Attunement)." We add overlay spans like
  `<span class="magic-item-rarity">Rare</span>` and
  `<span class="magic-item-attunement">Requires Attunement</span>` but
  the parenthetical, the comma, the period - all of it stays. Text
  content of the parent paragraph reads identically to the source.
- A creature's AC line stays as "AC 17 (Plate Armor)". We do not split
  it into separate fields for "armor class" and "armor type"; we wrap
  the value as `<td class="creature-armor-class">17 (Plate Armor)</td>`
  and let consumers parse the string if they want the split.

If a transformation would lose any visible text, it is wrong. Re-think
the transformation as adding structural overlay rather than restructuring.


## 8. Naming conventions

### CSS classes in the HTML

- Prefix every queryable value's class with its entity type:
  `spell-X`, `creature-X`, `magic-item-X`.
- Slug-case (lowercase, hyphen-separated).
- Use the full word, not abbreviations. Roy renamed `creature-ac` to
  `creature-armor-class`, `creature-hp` to `creature-hit-points`,
  `creature-cr` to `creature-challenge-rating`, and `creature-type` to
  `creature-kind` in v0.6.0+. The rationale: model fields should not
  be abbreviated. Do not introduce new abbreviated class names.
- `type` is reserved for the metalevel (the type of a value); use
  `kind` for domain categorization (the kind of creature). This
  follows Rust's reasoning for reserving `type` as a keyword and is
  generally good practice in any typed data system.

### Identifiers (HTML ids, file slugs)

- Slug-case. Always.
- For entity sections: `{entity-type}-{slug}` (e.g.
  `creature-goblin-warrior`, `spell-fireball`, `magic-item-bag-of-holding`).
- For magic item variants, the slug omits the "-1-2-or-3" suffix that
  appears in some titles (e.g. the +1/+2/+3 variants collapse into a
  single magic item with a `variants` field).

### Python lib property names

- Snake-case.
- Full words, matching the CSS class semantically. After v0.6.0+, the
  lib's `ac`/`hp`/`type`/`cr` get renamed to `armor_class`/`hit_points`/
  `kind`/`challenge_rating` to track the HTML rename. This is in my
  memory and should be done when the lib update happens.

### Document model identifiers (DOMMF/DOMQF)

- Snake-case for type names and field names.
- Slug-case for value content like slug fields.
- The DOMMF section of DOM-Contract.md is the canonical spec.


## 9. Subject vs topic markup

Inside descriptions, two distinct kinds of bold-italic structural labels:

- **Subject** (`<span class="subject">X.</span>`) - period-ending label,
  acts as a header for a sub-feature within a description. E.g.,
  "Alertness.", "Protective Aura.", "Spells." inside a Rod of Alertness.
  Originally rendered with `<strong><em>X.</em></strong>` in the SRD
  source; we converted to `<span class="subject">` in v0.6.0.
- **Topic** (`<span class="topic">X</span>`) - non-period-ending label,
  inline highlight of a defined term within prose. E.g., "Aberrations",
  "Beasts", "Celestials" in the creature-types listing. Originally
  rendered the same way in source; converted to `<span class="topic">`
  in v0.6.0.

Both are queryable structural markers. The distinction is grammatical
(period-terminator) but it also tracks intent (header-like vs
inline-highlight). The class name "topic" is intentionally generic so
that more specific renames can happen later if needed (e.g.,
"creature-type-name" if we want that particular semantic).

The two quoted glossary entries `"You."` and `"See Also."` are subjects
by manual override despite their textContent ending with `"` rather
than `.`. They function as glossary headers.


## 10. Em-dash and dash semantics

The SRD source uses em-dashes (Unicode em-dash, no surrounding spaces,
American typography) for parenthetical asides. The ASCII normalization
that converts em-dashes to plain hyphens must also add spaces around
them in the appropriate contexts, or the result is unreadable
("creatures-characters and monsters-have six abilities").

Final rules in the ASCII-normalized document:

- Range form (en-dash original): `A-Z`, `1-10`, no spaces. We use a
  single hyphen.
- Parenthetical aside form (em-dash original): ` - ` with single
  spaces on each side. We use a single hyphen with spaces.
- Minus sign (Unicode minus original): same as parenthetical for math
  expressions; same as range for negative numbers (`-3` not `- 3`).

Detection of parenthetical asides is contextual and not character-level.
The heuristic that works:

- `word-CONNECTIVE` where CONNECTIVE is a function word (article,
  preposition, conjunction, aux verb, or common adverb like "also,"
  "particularly," "called," "described"). This catches the CLOSING dash
  of an em-dash pair around an aside.
- `word-word AND/OR/BUT/NOR` where the second word is a noun and the
  third word is a connector. This catches the OPENING dash of a pair
  around a noun-list aside.
- Multi-part hyphenated compounds (`out-of-the-way`, `hard-to-reach`,
  `5-foot-by-5-foot`, `CC-BY-4.0`) are detectable because the second
  word is followed by another `-`. Skip these; they are legitimate.
- A small whitelist of two-part legitimate compounds whose second word
  is a connective: `agreed-upon` is the only one I have found so far.
  Skip these too.

If I run this normalization on a future fresh source, I will catch
about 95% with the function-word detection, another 4% with the
noun-noun-AND detection, and the last 1% by surveying paragraphs that
contain both `word - word` (with spaces) and `word-word` (without) and
manually triaging the unspaced patterns against a known-legit-compound
filter.

When in doubt, the noun-noun-AND detection has false positives on
legitimate compounds like `Two-Handed`, `one-half`, `one-way`,
`self-propelled`; those are filtered by the multi-part check or a
small whitelist.


## 11. Known pitfalls and how to avoid them

### lxml encoding gotcha

`lxml.html.fromstring(bytes)` misdecodes UTF-8 input as Latin-1 and
produces double-encoded mojibake on `tostring()`. **Always pass strings
(already UTF-8 decoded) to fromstring(), not raw bytes.**

The mojibake patterns to watch for if I forget:

- `0xC3 0xA2 0xC2 0x80 0xC2 0xXX` - en/em-dashes, curly quotes,
  ellipsis, bullet
- `0xC3 0xA2 0xC2 0x88 0xC2 0x92` - minus sign
- `0xC3 0x82 0xC2 0xBD` - one-half
- `0xC3 0x82 0xC2 0xAD` - soft hyphen
- `0xC3 0x83 0xC2 0x97` - multiplication sign

If I see these in the file, a previous lxml round-trip went wrong. Fix
with byte-level pattern replacement before doing anything else.

### lxml strips pre-root comments

The `<!-- NOTE FOR AI AGENTS -->` block at the top of srdom.html is
not preserved by `lxml.html.tostring()` by default. Any round-trip
through lxml will lose it. The for-ai-agents section in the body is
the canonical source; if the comment goes missing, regenerate it from
that section's content.

### Regex catastrophic backtracking on 2.8 MB files

Patterns with `.*?` against the full document text can lock up
indefinitely. Symptoms: the script times out or takes minutes for
what should be a millisecond match. Fix: use substring searches
(`str.find`, `str.index`) to locate boundaries first, then operate
on the bounded substring with regex.

Specifically, avoid:

```python
re.search(r'<p>.*?some_marker.*?</p>', huge_text, re.DOTALL)
```

Prefer:

```python
pos = huge_text.find('some_marker')
p_start = huge_text.rfind('<p>', 0, pos)
p_end = huge_text.find('</p>', pos) + len('</p>')
inner = huge_text[p_start+3:p_end-4]
```

### Anthropic web_fetch cache lag

The cache that backs web_fetch for the published site lags reality.
If I fetch `srdom.html` and the version meta says `0.4.0` but Roy says
`0.5.0` is live, trust Roy. Mention the cache lag and proceed.

The cache holds more than just body content - it also holds response
headers, including Content-Type. Observed in practice: when Roy added
a `_headers` file to the repo to serve `srdom.py` and `srdom.nu` with
`Content-Type: text/plain`, the change took effect immediately for
direct clients (his wget confirmed `text/plain`), but Anthropic's
web_fetch continued seeing `application/octet-stream` from its cached
entry. The fix waits for cache expiry; nothing on the user or origin
side forces it.

The cache is keyed on the URL path, ignoring query parameters.
Cache-busting via `?v=1` or similar does not work - Anthropic's
fetcher strips the query string before issuing the request, so the
cache key is identical to the no-query version. I confirmed this by
fetching `https://srdom.sourcetrait.pub/srdom.py?v=1` and seeing
`destination_url` come back as the bare path with no query string.

### Cloudflare AI bot blocking (Wall 2 after the URL allowlist)

Even after I clear the web_fetch URL allowlist (Wall 1), Cloudflare's
bot defenses can still block the request at the network layer. The
srdom.sourcetrait.pub site sits behind Cloudflare, and Cloudflare's
defaults as of mid-2025 are aggressive:

- **Bot Fight Mode** is enabled by default on all plans (including free)
  and classifies ClaudeBot, GPTBot, PerplexityBot, etc. as bots to be
  challenged or blocked.
- **"Block AI Bots"** is the default for newly created Cloudflare
  domains since mid-2025, blanket-blocking categorized AI crawlers.

Symptom: fetch returns a challenge page, 403 body, or a Cloudflare
"You are unable to access" page in the content - not a tool-level
permission error. If a fetch from this domain returns weird HTML
instead of the expected content, this is the likely cause. The fix
is for Roy to allow ClaudeBot in Cloudflare's Security settings or
to put a permissive robots.txt for ClaudeBot in place.

### Confabulating biographical or attribution facts

When stating someone's name, role, affiliation, or other biographical
fact, check the actual source in my current context rather than
trusting associative recall. I once wrote "Roy Ratcliffe" in CLAUDE.md
when the correct name "Roy Laurie" was in a fetched sourcetrait.com
page I'd looked at minutes earlier. The pattern is dangerous because
the wrong answer feels plausible and confident. Always verify against
the live context before writing.

### File handles to /home/claude/build vs /mnt/user-data/outputs

The build directory is my scratchpad. The outputs directory is what
Roy sees via present_files. Always copy the final version to outputs
before presenting; do not point present_files at the build directory
directly (the user does not see it).


## 12. Common operations cheat sheet

### Get the version of the current srdom.html

```bash
grep -oP '<meta name="version" content="\K[^"]+' srdom.html
```

Or via lib:

```python
import srdom
print(srdom.load(source="srdom.html").version)
```

### List CSS class names matching a prefix

```python
import re
classes = set()
with open('srdom.html') as f:
    text = f.read()
for m in re.finditer(r'class="([^"]+)"', text):
    for c in m.group(1).split():
        if c.startswith('creature-'):
            classes.add(c)
for c in sorted(classes):
    print(c)
```

### Integrity check (the standard one)

```python
import re
from collections import Counter
with open('srdom.html') as f:
    text = f.read()
ids = re.findall(r'id="([^"]+)"', text)
dupes = {k: v for k, v in Counter(ids).items() if v > 1}
all_ids = set(ids)
hrefs = re.findall(r'href="#([^"]+)"', text)
broken = sorted(set(h for h in hrefs if h not in all_ids))
print(f"dupes={len(dupes)}, broken={len(broken)}")
print(f"spells={text.count('<section class=\"spell\"')}, "
      f"creatures={text.count('<section class=\"creature\"')}, "
      f"magic-items={text.count('<section class=\"magic-item\"')}")
```

Expected: 0 dupes, 0 broken, 339 spells, 336 creatures, 257 magic items
(for SRD 5.2.1; future SRD versions will have different counts).

### Run the lib self-test

```bash
python3 srdom.py --source ./srdom.html test 2>&1 | tail -20
```

### Substring-safe class rename

```python
import re
text = re.sub(rf'\b{re.escape(old)}\b', new, text)
```

The `\b` word boundaries prevent collisions like `creature-ac` matching
inside `creature-action`. Verify with a count comparison before and
after.


## 13. Outstanding TODOs

These are items in flight that have not yet been written into the
for-ai-agents section or otherwise made durable. Once they are done,
remove them from here.

- **srdom.py creature property renames (post v0.6.0 class rename).**
  The HTML classes were renamed `creature-ac`/`hp`/`type`/`cr` to
  `creature-armor-class`/`hit_points`/`kind`/`challenge_rating`. The
  lib still uses the old short property names and the old XPaths. When
  this is updated, rename properties AND XPaths together; do not leave
  the lib using short names while querying long ones.
- **srdom.nu surface check after the lib update.** The Nushell wrapper
  does not directly reference class names, but it does expose lib
  output records whose field names will change. Verify field names in
  the output records after the lib update.
- **DOM-Contract.md completion.** Roy is drafting the formal model
  spec. DOMMF section is mostly done; DOMQF is in early shape. trait
  and action types are referenced in the creature model but not yet
  defined. Cardinality rules (singular type takes first, vec<T> takes
  all) are inferred from DOMMF type but not yet written into the spec.
- **Cell-merge defects in the damage glossary table.** Originally
  flagged during a survey; turned out to be a false positive from the
  survey script (text_content concatenation across cells). No fix
  needed; if I re-survey in the future and flag this again, it is
  the same false positive.
- **Player-class to creature model merge.** When player-class
  entities get modeled, several fields are merge candidates: gear,
  proficiency_bonus, xp, hit_dice, damage_immunities/resistances/
  vulnerabilities, condition_immunities, bonus_actions, reactions,
  spellcasting. Three groups by merge effort:
  1. Direct exposure (add fields, no conceptual merge needed):
     gear, proficiency_bonus, xp, hit_dice, the immunities, bonus_actions,
     reactions.
  2. Shared sub-type (define `spellcasting` data type used by both
     creature and player-class entities).
  3. Derivation alignment (decide whether creatures expose derived
     saving_throw_proficiencies and skill_proficiencies to match how
     player classes declare them, or leave the models shaped
     differently).


## 14. Where to look for more

- **`for-ai-agents` section in srdom.html** (specifically the
  `ai-agent-parsing-intro` sub-section and future siblings) - the
  in-document operational handbook for parsing and rebuilding SRDOM.
  Read this if I need to understand the data model, query patterns,
  or normalization rules.
- **`DOM-Contract.md`** - the formal DOMMF + DOMQF spec. Read this if
  I am about to design or extend the data model.
- **The published lib (`srdom.py`)** - the reference parser. Read this
  if I am unsure how a particular field should be queried or what its
  expected shape is.
- **Past conversations** - I have `conversation_search` and
  `recent_chats` tools available. If Roy references a prior decision
  or a "we discussed" pattern, search before asking him to repeat.

In approximate order of authority for any given question:

1. Roy's explicit instruction in the current conversation.
2. This file (CLAUDE.md) for workflow and convention questions.
3. The for-ai-agents section for parsing and rebuilding questions.
4. DOM-Contract.md for data-model questions.
5. The lib's existing behavior, as a tiebreaker for ambiguous spec
   questions.


## 15. Updating this document

I should edit this file whenever:

- I learn a pattern that bit me and would bite a future me.
- A convention changes (rename, restructure, deprecation).
- A TODO becomes a permanent rule (move it from section 13 to its
  proper home elsewhere).
- A TODO is completed (remove it).
- Roy gives an instruction that should generalize beyond the immediate
  session.

Edits should preserve the section ordering and headings. New patterns
that do not fit anywhere can become new sections, but I should resist
the urge to over-fragment.

**Bump the revision marker** at the top of the file whenever I make a
meaningful edit. Simple monotonic integer. Update the date alongside.
The marker is for fast orientation when reading the file cold: if my
memory tells me I last wrote revision N and the file says N+5, the
file has moved since I last touched it and I should re-read it before
trusting prior assumptions. Git provides the diff history; the marker
just provides a fast at-a-glance "is this current?"

When I edit this file in a session, I leave it in
`/home/claude/build/CLAUDE.md`, copy it to outputs, present it to Roy,
and let him commit. The file does not auto-deploy or get included in
the published site; it is repo-only.


---

End of file. If I am Claude reading this in a fresh session: welcome
back. The project is in good shape. Read the `for-ai-agents` section
of srdom.html next to load the data-model context, then ask Roy what
he wants to work on.
