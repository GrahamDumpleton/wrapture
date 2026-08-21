# Design philosophy

wrapture attaches bindings to call sites in code you do not want to, or
cannot, modify. One mechanism scales from simple monkey patching, through
testing, to tracing a live application. This page explains the thinking
behind that mechanism, and how it relates to `unittest.mock`.

## The code stays real

The foundational choice is to **wrap rather than replace**. A binding
installs a wrapt wrapper around the real callable; unless you configure
behaviour that says otherwise, the real code runs.

That choice has consequences that run through the whole library:

- You can intervene surgically: rewrite one argument, or one field of a
  result, while everything else executes for real.
- Calls an object makes to itself pass through the wrapper too, so nothing
  is structurally invisible the way it is to a substituted double.
- Removing the wrapper restores the original exactly, and the wrapper
  composes with wrappers other parties installed on the same target.

## Loud failure over silent wrongness

A patching tool that misfires quietly produces tests that pass for the
wrong reason. wrapture's API is shaped to make mistakes noisy:

- A misspelled attribute name raises at `binding()` creation, on the line
  that made the mistake, not later as a patch that never fires.
- Mixing the two lifecycle styles raises `AlreadyAppliedError` rather than
  letting an inner scope silently remove a patch an outer scope owns.
- `active` is queried, not cached: if something else replaces or unwraps
  the target behind your back, the binding reports `displaced` instead of
  claiming the patch is in place.
- Calls skipped while suspended are counted on `suspended_calls`, so a
  quiet patch can be told apart from a broken one.

## Declaration is free, effect is explicit

`binding()` declares; it never patches. Bindings can be created at class
or module scope, stored, and reused across apply/remove cycles. Effects
happen only at explicit points: `apply()` installs, `remove()` restores,
`suspend()` disables in place. Each axis is independent, and each is
reversible.

## Built on wrapt, not hiding it

wrapture does not reimplement patching; it adds lifecycle and vocabulary
over wrapt, and every binding exposes the underlying handle through its
`wrapper`, `target` and `name` attributes, so anything core wrapt can do
remains available.
If wrapture's API does not cover a case, you can always drop down a level.

## wrapture and unittest.mock

The short version: `unittest.mock` substitutes objects and asserts on
what the substitute recorded; wrapture wraps the real code and
intervenes or observes in flight, strictly and recorded, with
substitution available as a deliberate opt-in (`stub()` for one
callable, `mock(Spec)` for one collaborator) rather than the silent
baseline. The two coexist happily in one suite. The
[Coming from unittest.mock](coming-from-mock.md) page walks the
comparison case by case, with the same test written both ways, and
ends with the one kind of double wrapture deliberately does not
provide.
