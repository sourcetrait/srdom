# DOM Contract

This document establishes a data modeling contract with its reader.

It uses a **DOM Modeling Format** to define data models and a corresponding
**DOM Querying Format** to define an interface that interacts with those
models.


## DOM Modeling Format
The `DOM Modeling Format` (`DOMMF`) used in this document defines data types
within a hierarchy.

All identity tokens use a *snake* *string format* (snake-case).

All structural indentation is standardized at 4 space (` `) characters.

A `Type` is enumerated:
- *Data Type*
- *Native Type*

A `Data Type` is uniquely named with an identity token.  
A *data type* encapsulates a set of *fields* with the `{` and `}` characters.  
Child *fields* are separated by a `,` character and a newline.

A `Field` definition consists of a name (*identity token*) and
a *value type* (*type*).  
It must be indented once within its parent *data type*.
Its name must be unique within the scope of its parent *data type*.  
Its name and value type are separated by a `:` character.
Its *value type* is any other *data type* or *native type*.

`Native Type`s are enumerated:
- `string` :: UTF-8 
- `string(string_format)` :: UTF-8 `string` that adheres to a defined
*string format* by its name.
- `bool` :: Boolean value of either `true` or `false`
- `i32` :: A 32-bit signed integer.
- `u32` :: A 32-bit unsigned integer.
- `f32` :: A 32-bit signed floating point number.
- `vec<type>` :: A vector (dynamic array) of *type* items.

A `String Format` is a set of rules that a string must adhere.

Available *string formats* are enumerated:
- `slug` :: slug-case. e.g.: this-is-a-valid-slug
- `snake` :: snake-case. e.g.: this_is_a_valid_snake 


## DOM Querying Format (DOMQF)

The `DOM Querying Format` mirrors the `DOM Modeling Format` with slight
changes.

Each *data type* definition here must mirror the structure of an
existing DOMMF model. A DOMMF model that lacks a corresponding DOMQF definition
is not expected to be easily queried directly from the DOM.

Work in progress: Actual description of format.


## SRDOM Model (DOMMF)
```
type creature {
    slug: string(slug),
    name: string,
    size: string,
    kind: string,
    alignment: string,
    armor_class: string,
    initiative: string,
    hit_points: string,
    speed: string,
    strength: string,
    strength_modifier: string,
    strength_save: string,
    dexterity: string,
    dexterity_modifier: string,
    dexterity_save: string,
    constitution: string,
    constitution_modifier: string,
    constitution_save: string,
    intelligence: string,
    intelligence_modifier: string,
    intelligence_save: string,
    wisdom: string,
    wisdom_modifier: string,
    wisdom_save: string,
    charisma: string,
    charisma_modifier: string,
    charisma_save: string,
    skills: string,
    senses: string,
    languages: string,
    challenge_rating: string,
    traits: vec<trait>,
    actions: vec<action>,
    legendary_actions: vec<action>,
}
```

## SRDOM Query Interface (DOMQF)
```
node creature {
    key {
      field: slug,
      prefix: 'creature-',
    },
    fields {
        slug: key,
        name: query relative {
          selector: '.creature-name',
          xpath: union [
              './h3[@class="creature-name"]/text()',
              './h5[@class="creature-name"]/text()',
          ],
        },
        // continues ...
    }
}
```

