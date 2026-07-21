"""Shared pytest fixtures and constants.

De-duplicated test constants live here so two test modules can never drift out
of sync.  ``SYSTEM_HEALTH_DESCRIPTOR_FIELDS`` is the single source of truth for
the closed field set an :class:`~workbench.system_health.IntegrationDescriptor`
may serialize; ``tests/test_api.py`` and ``tests/test_security_contract.py``
both import it, so the descriptor's leak-by-addition guard is stated once.
"""
from __future__ import annotations

#: The exact closed field set a system-health descriptor may serialize.  A field
#: added outside this set (a leak-by-addition) must fail the response/descriptor
#: tests, so the assertion is not a tautology.  Kept here, imported by both the
#: API surface test and the security-contract test, so the two can never drift.
SYSTEM_HEALTH_DESCRIPTOR_FIELDS = frozenset({
    "configured", "dependencies", "digest", "integration_id", "non_canonical",
    "owner", "remediation", "schema_version", "state", "title",
    "version", "detail", "last_checked_at",
})
