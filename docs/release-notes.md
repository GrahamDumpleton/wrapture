# Release notes

## Version 1.0.0b4

- `wrapture.Part` is renamed `wrapture.Aspect`, and the resolved form
  on the instance `AspectSettings`, the config file being unchanged.
  Reviewed in the packages that declare them, `Part` did not read
  right beside `Instrumentation`, and an aspect of what the
  instrumentation covers is a better fit for a thing that spans many
  call sites. The rename lands hours after 1.0.0b3 introduced the
  class, so there is no alias for the old name. See
  [aspects](instrumentation-packages.md#aspects).

## Version 1.0.0b3

- A new `shape` capture level, between `reference` and `summary`. A
  string or container records as its type and size (`<dict 3 keys>`,
  `<list 40 items>`, `<str 5120 chars>`) and never its contents; an
  atomic value records as itself and anything else as the bounded
  summary. It is the level for values that are data rather than
  objects, such as the result a framework turns into a response body.
  See [how much is captured](unit-testing.md#how-much-is-captured).

- Instrumentation parts. An instrumentation declares the groups of
  call sites it binds as `wrapture.Part` values in its `settings`,
  each with a switch, recording defaults and settings of its own. The
  config addresses a part as a sub-table of the `[[instrument]]` entry
  (`[instrument.views]`) using the keys an `[[observe]]` entry takes,
  so `capture_result`, `redact`, `leaf` and the rest mean the same
  thing everywhere. A package may mark one part primary, whose keys
  may be written flat on the entry, and a bare boolean under a part's
  name is its switch. The listing tool and the generated template
  print the parts. See [aspects](instrumentation-packages.md#aspects).

## Version 1.0.0b2

- `[[observe]]` entries take `capture`, `capture_args` and
  `capture_result`, the `binding()` options of the same names. An
  entry's own level beats the config's top-level `capture`, and a
  `redact` list composes over whatever level the arguments axis
  resolves to.

- `[[observe]]` entries also take `stack`, `redact_result` and
  `redact_marker`. Behind `redact_result`, `redact()` with no names
  now masks every value on whichever axis it sits, so
  `capture_result=wrapture.redact()` hides a result while the tape
  still shows the call returned.

## Version 1.0.0b1

The first beta, and the first release meant for use beyond the
project's own development. Everything is new in this version, so
rather than listing changes, see the rest of the documentation for
what the library provides, starting with
[getting started](getting-started.md).
