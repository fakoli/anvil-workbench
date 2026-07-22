"""Shared pytest fixtures for the test suite.

The hermetic run-context factory and the closed system-health descriptor field
set live in :mod:`tests._support` (a plain importable module, so they survive a
future ``tests/__init__.py`` or ``importmode=importlib``).  This module only
adapts them into pytest fixtures and re-exports the names that older imports
(``from conftest import ...``) still reference, so nothing breaks while new code
imports directly from ``_support``.
"""
from __future__ import annotations

from typing import Callable

import pytest

from _support import (  # noqa: F401  (re-exported for `from conftest import ...`)
    SYSTEM_CONFIGURATION_DESCRIPTOR_FIELDS,
    SYSTEM_HEALTH_DESCRIPTOR_FIELDS,
    build_run_context,
    compile_delivery_snapshot,
    load_example,
)
from workbench.models import RunContext
from workbench.workflow_snapshot import WorkflowSnapshot


@pytest.fixture
def run_context_snapshot() -> WorkflowSnapshot:
    return compile_delivery_snapshot()


@pytest.fixture
def make_run_context() -> Callable[..., RunContext]:
    """A factory the tests call with overrides to build a run context."""
    return build_run_context
