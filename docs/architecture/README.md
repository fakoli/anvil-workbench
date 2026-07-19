# Architecture resources

This directory turns the Workbench product framing into small, implementation
facing documents. Read the shortest applicable document first rather than
loading every design note into a model context.

| Document | Read it when... |
| --- | --- |
| [Research and alternatives](RESEARCH.md) | Choosing a workflow runtime, UI/product boundary, catalog shape, or deployment pattern. |
| [Runtime inference](RUNTIME-INFERENCE.md) | Designing how a smaller local model receives context, chooses an approved action, and learns from receipts. |
| [Workflow operation layer](../WORKFLOW-OPERATION-LAYER.md) | Implementing the cross-product State/Serving operation catalog and workflow v2. |
| [Integration contracts](../CONTRACTS.md) | Changing an implemented boundary or checking an authority rule. |
| [Contract resources](../contracts/README.md) | Adding a typed catalog, workflow, bridge command, receipt, or run-context resource. |
| [Digesting and snapshots](../contracts/DIGESTING.md) | Implementing or reviewing immutable catalog/profile/workflow snapshots. |

## Architecture position

Workbench is **not** a fleet of microservices. It is a modular hub (API,
durable store, scheduler, redaction, and projection can ship together) connected
to independently owned systems through narrow contracts:

```text
browser --tailnet identity--> Workbench hub --outbound commands--> project bridge
                                  |                                  |
                                  |                                  +--> State CLI/events
                                  |                                  +--> Codex + Serving Responses
                                  |                                  +--> local GitHub credential
                                  v
                           Postgres + derived Neo4j
```

State, Serving, and the bridge are authority/data-plane boundaries. They are not
licenses to duplicate their implementation in Workbench. Split the hub into
separate deployables only when a measured bottleneck, separate security domain,
or independent release cadence requires it.

## How to make an architecture decision

1. Name the owner of the state transition or external effect.
2. Decide whether Workbench needs to observe, request, or own it. It should
   almost always only request or observe cross-product effects.
3. Choose a typed, versioned descriptor and receipt before a transport.
4. Add an explicit preview, approval, idempotency, timeout, and reconciliation
   rule when the effect is not read-only.
5. Add a hermetic contract test before a UI control. A button without an owned
   operation, receipt, and failure state is not a feature.

## What belongs here

Put decision records, research comparisons, dataflow diagrams, and runtime
inference designs here. Put executable schemas and examples in
[`docs/contracts/`](../contracts/README.md), implementation behavior in tests,
and current qualification results in [QUALIFICATION.md](../QUALIFICATION.md).
