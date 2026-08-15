"""Sphinx configuration for the wrapture documentation."""

import os
import sys

# Make the package importable directly from the source tree, so the version
# is always read from the code being documented.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

from wrapture import __version__  # noqa: E402

project = "wrapture"
author = "Graham Dumpleton"
copyright = "2026, Graham Dumpleton"  # noqa: A001

version = __version__
release = __version__

extensions = [
    "myst_parser",
]

myst_heading_anchors = 2

exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"
