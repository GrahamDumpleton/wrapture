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

# Heading anchors to depth 3, so a guide's subsections (the h3 level) can
# be linked from other pages by their slug.
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"

# Give every page an "Edit on GitHub" link pointing at its source in the
# repository, so readers can reach the project from wherever they land.
html_context = {
    "display_github": True,
    "github_user": "GrahamDumpleton",
    "github_repo": "wrapture",
    "github_version": "develop",
    "conf_py_path": "/docs/",
}
