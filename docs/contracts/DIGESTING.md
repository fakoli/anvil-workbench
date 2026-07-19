# Contract digesting and immutable snapshots

This is the normative digest rule for the **proposed** operation layer. It
exists so a hub and a project bridge can independently derive the same value.
It does not alter the current v1 bridge protocol.

`workbench.contracts` is the reference implementation. A future bridge must
use byte-for-byte compatible behavior and reject a resource that does not
recompute to its advertised digest.

## Canonical bytes

For all resources, serialize JSON as UTF-8 with object keys sorted
lexicographically, no insignificant whitespace, `ensure_ascii=false`, and no
non-finite numbers. This contract domain permits JSON strings, booleans,
integers, arrays, and objects; floating-point values are not permitted in
digest-bearing resource fields. Prefix those bytes with the exact ASCII domain
separator below, including its trailing NUL byte, then calculate SHA-256 and
render it as lowercase `sha256:<64 hex>`.

| Resource | Prefix | Excluded or normalized fields |
| --- | --- | --- |
| Operation | `anvil-workbench/operation/v1\0` | Omit its `operation_digest`. |
| Provider catalog | `anvil-workbench/catalog/v1\0` | Omit `catalog_digest` and volatile `generated_at`; sort `operations` by `(id, contract_version, operation_digest)`. |
| Capability profile | `anvil-workbench/capability-profile/v1\0` | Omit `digest`; sort operation allowlist entries, skill entries, model profiles, and approval actions. |
| Workflow | `anvil-workbench/workflow/v2\0` | Omit an optional future `digest`; preserve step and edge order. |
| Skill | `anvil-workbench/skill/v1\0` | Omit its declared digest; the bridge hashes configured reviewed skill metadata/content before it publishes a reference. |
| Approved operation inputs | `anvil-workbench/approval-payload/v1\0` | Hash the exact typed `inputs` object attached to the approval-gated operation. |

An operation digest is checked first, then its enclosing catalog digest. The
catalog includes each checked operation digest. A provider must not use
`generated_at` to change a snapshot: it is provenance only, not digest input.

## Snapshot verification order

The configured bridge files are the local trust root; schema-valid hub input
does not create trust. Before an effect, a bridge must:

1. Recompute and recognize every locally configured catalog and the selected
   capability profile.
2. Require exactly one snapshot catalog entry for the selected operation
   provider, with the local catalog's digest. Reject duplicate, absent, or
   changed provider entries.
3. Require the selected `(provider, id, contract_version, operation_digest)` in
   both that catalog and the profile's operation allowlist.
4. For a human-approval-gated operation, require an unexpired one-time grant,
   the catalog's declared `approval_action`, and an approval payload digest of
   the exact typed operation inputs. The bridge uses an injected hub-backed
   approval consumer to consume the grant atomically immediately before the
   adapter effect; command fields that merely claim a grant are not authority.

Signatures are intentionally deferred. Until they are added, an operator's
configured local catalog/profile bytes and authenticated bridge enrollment are
the trust root—not an unsigned browser or hub payload.
