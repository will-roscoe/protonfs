# Configuration file for the Sphinx documentation builder.
import os
import sys

# -- Path setup ---------------------------------------------------------------
sys.path.insert(0, os.path.abspath("../src"))

# -- Project information ------------------------------------------------------
project = "protonfs"
author = "Will Roscoe"
copyright = "2026, Will Roscoe"
release = ""

# -- General configuration ----------------------------------------------------
extensions = [
    # Core
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",  # NumPy/Google docstring styles
    # Python
    "sphinx.ext.autodoc",
    "sphinx_autodoc_typehints",
    "sphinx_click",  # autodoc for the Click CLI (command/option/envvar directives)
    "sphinx_copybutton",
    # Hover previews on cross-references. Rendered client-side at build time (unlike
    # sphinx-hoverxref, which needs the Read the Docs Embed API), so tooltips work on
    # our GitHub Pages deploy with no external service.
    "sphinx_tippy",
]

templates_path = ["_templates"]
# `_shared/` holds fragments pulled into other pages via `.. include::`; they are not
# standalone documents, so keep them out of the toctree/build (avoids "not in any
# toctree" warnings).
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "_shared/**"]
source_suffix = ".rst"
master_doc = "index"

# -- Options for HTML output --------------------------------------------------
html_theme = "furo"
html_static_path = ["_static"]
# The logo is theme-neutral (white git glyph on the Proton gradient), so it serves
# furo's light and dark sidebars. The favicon uses the white-tile variant so it stays
# legible on both light and dark browser chrome.
html_logo = "_static/logo.svg"
html_favicon = "_static/logo-white.svg"

# -- Intersphinx --------------------------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- sphinx-tippy (hover previews) --------------------------------------------
# Collect heading-anchor tooltips only from furo's main content wrapper, so hovering
# a link in the nav sidebars doesn't pop a tooltip of the sidebar entry itself.
# Cross-reference tooltips (:ref:/:doc:, e.g. the command links in the task guide)
# are generated regardless of this selector.
tippy_anchor_parent_selector = "div.content"
# Skip the "¶" heading permalinks -- they'd otherwise get a tooltip of their own section.
tippy_skip_anchor_classes = ("headerlink",)
tippy_props = {
    "placement": "auto-start",
    "maxWidth": 500,
    "interactive": True,
    "theme": "light-border",
    "delay": [200, 0],
}
