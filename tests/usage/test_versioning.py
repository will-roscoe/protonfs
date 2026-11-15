"""Every public free function of :mod:`protonfs.usage` says when it was added.

A class-level ``versionadded`` covers its methods (ruling R7); free functions need their own.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import protonfs.usage


def _public_functions():
    for info in pkgutil.iter_modules(protonfs.usage.__path__):
        module = importlib.import_module(f"protonfs.usage.{info.name}")
        for name, obj in vars(module).items():
            if (name.startswith("_") or not inspect.isfunction(obj)
                    or obj.__module__ != module.__name__):
                continue
            yield f"{module.__name__}.{name}", obj


@pytest.mark.parametrize(("qualname", "fn"), list(_public_functions()))
def test_public_usage_function_has_versionadded(qualname, fn):
    assert ".. versionadded::" in (fn.__doc__ or ""), qualname
