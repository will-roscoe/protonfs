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
    "sphinx_copybutton",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
source_suffix = ".rst"
master_doc = "index"

# -- Options for HTML output --------------------------------------------------
html_theme = "furo"
html_static_path = ["_static"]

# -- Intersphinx --------------------------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}
