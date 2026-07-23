"""Shared base error for the Workbench operational store and its per-domain stores.

Kept in a dependency-free leaf module so the core store and every extracted
per-domain store (operation/skill-adoption/preference/plugin-preference) can share
one error base without a circular import.
"""
from __future__ import annotations


class StoreError(RuntimeError):
    """A requested Workbench operation violates an immutable audit invariant."""
