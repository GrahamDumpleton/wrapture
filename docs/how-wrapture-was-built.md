# How wrapture was built

I am Graham Dumpleton, the author of wrapture, and also of wrapt and
mod_wsgi. Every line of code and documentation in wrapture, this page
included, was written by an AI assistant working under my direction. I
want to be upfront about that, and equally upfront that this was not a
one-shot prompt and a pile of generated code. It was a long, deliberate
endeavour, and I think the process is what makes the result worth
trusting or not, so this page explains it.

## The itch

Through [wrapt](https://github.com/GrahamDumpleton/wrapt), the library
wrapture is built on, I have spent a good part of two decades on the
mechanics of monkey patching in Python. For much of that
time I have carried a quiet dissatisfaction with `unittest.mock`: not
that it is bad, but that it falls a little short on correctness. A
fabricated object answers every method and verifies nothing, a patched
call records a flat list with no return values and no nesting, and the
things a test most needs to be sure of, that the right calls happened,
in the right order, with the real code actually running, sit just
outside what substitution can express.

I had long believed that wrapt's machinery, wrapping real code rather
than replacing it, could support something better: a testing tool where
the real call graph stays intact, everything that flows through it is
recorded and introspectable, and strictness is the default rather than
an opt-in. wrapture is me finally scratching that itch, and satisfying
my curiosity about whether the idea held up.

## The process

The work started well before any code. I spent days specifying what I
wanted: the goals, the scope, the shape of the API, and the layers it
would be built in, written down as design documents that the AI and I
argued over and refined before implementation began.

From there the work proceeded in deliberate layers, each one specified,
discussed, implemented, tested and documented before the next began.
Two disciplines did most of the steering. Documentation grew with the
code rather than after it, and every example in these pages runs as a
doctest, so the docs are continuously proven against the
implementation, and writing them repeatedly exposed designs that read
worse than they demoed. And the test suite runs against every supported
Python version, including the free-threaded builds, on every change.

The step I would most recommend to anyone attempting something similar
came late: validation against reality. I took the unit test suites of
well-known Python packages that lean heavily on `unittest.mock` and had
the AI replicate their tests using wrapture instead, side by side with
the originals. Every point of friction became a decision: sometimes a
documented position on why wrapture deliberately differs, and sometimes
a missing feature that got specified, built and documented like
everything else. Several of the pieces described in these docs exist
because a real test suite could not be expressed cleanly without them.

Throughout, the division of labour was consistent: the AI wrote the
code, the tests and the prose; I set the direction, made the design
calls, reviewed what came back, and sent plenty of it back. My
experience with wrapt and with Python's darker corners is all through
the result, in what was asked for as much as in what was refused.

## A clean slate

One advantage a new library has over an old one is coherence. Mature
packages accrete: features are added one release at a time, each
sensible alone, and there is never a moment when the whole API can be
redesigned to match what was learned along the way. Because wrapture's
functionality arrived in a compressed period with the whole design
still in view, its API could be kept consistent from end to end:
naming conventions hold across the entire package with no exceptions,
because nothing predates them, the same concepts reappear wherever
they apply rather than each corner growing its own variant, and when
validation showed a design could be better, it was redesigned rather
than worked around.

## If you would rather not

Some people are firmly opposed to AI-written software. If that is you,
I understand, and I am not going to argue with your position; it is a
reasonable one to hold, and this page exists so you can make the call
with the facts in hand rather than discover them later. If AI
involvement rules wrapture out for you, then wrapture is not for you,
and no hard feelings whatsoever. The code of wrapt, written the old
way, remains underneath.
