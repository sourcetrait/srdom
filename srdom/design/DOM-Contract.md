# DOM Contract

This document establishes a data modeling contract with its reader.

It uses a **DOM Modeling Format** to define data models and a corresponding
**DOM Querying Format** to define an interface that interacts with those
models.


## DOM Modeling Format
The `DOM Modeling Format` (`DOMMF`) used in this document defines data types
within a hierarchy.

Line comments start with `//`.

Block comments are encapsulated between `/*` and `*/`.

All identity tokens use a *snake* *string format* (snake-case).

All structural indentation is standardized at 4 space (` `) characters.

`Type` is an abstract that is concretely enumerated as:
- *Data Type*
- *Enum Type*
- *Model Type*

A `Model Type` is uniquely named with an identity token.  
The `model` keyword begins its definition.
A *model type* encapsulates a set of *fields* with the `{` and `}` characters.  
Child *fields* are separated by a `,` character and a newline.

A `Field` definition consists of a name (*identity token*) and
a *value type* (*type*).  
It must be indented once within its parent *model type*.
Its name must be unique within the scope of its parent *model type*.  
Its name and value type are separated by a `:` character.
Its *value type* is any *Type*.

`Enum Type` is a fieldless enumeration type. It consists of a unique identity
token as a name that encapsulates a set of uniquely identified variants between
the `{` and `}` characters. Each variant is separated by a `,` character.  
The `enum` keyword begins its definition.


`Data Type`s are enumerated:
- `string` :: UTF-8 
- `string(string_format)` :: UTF-8 `string` that adheres to a defined *string format* by its name.
- `bool` :: Boolean value of either `true` or `false`
- `i32` :: A 32-bit signed integer.
- `u32` :: A 32-bit unsigned integer.
- `f32` :: A 32-bit signed floating point number.
- `vec<type>` :: A vector (dynamic array) of *type* items.
- `map<type, type>` :: A key/value map between two types.
- `option<type>` :: An optional type enumerated as:
  - `none` :: no value (null)
  - `some<type>` :: some value, containing *type*

A `String Format` is a set of rules that a string must adhere to.

Available *string formats* are enumerated:
- `slug` :: slug-case. e.g.: this-is-a-valid-slug
- `snake` :: snake-case. e.g.: this_is_a_valid_snake 
- `md` :: Markdown (CommonMark) format. e.g.: `**this** *is* a [valid](#something) snake`

## DOM Querying Format (DOMQF)

The `DOM Querying Format` mirrors the `DOM Modeling Format` with slight
changes.

Each *model type* definition here must mirror the structure of an
existing DOMMF model. A DOMMF model that lacks a corresponding DOMQF definition
is not expected to be easily queried directly from the DOM.

Work in progress: Actual description of format.
