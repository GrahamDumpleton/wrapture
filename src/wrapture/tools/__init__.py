"""Command line tools shipped with wrapture.

One entry point with subcommands, the pip and coverage convention:
python -m wrapture.tools lists the available commands, and
python -m wrapture.tools COMMAND runs one. Each command is a thin
shell over the public API, so anything a command does is equally
available to code.
"""
