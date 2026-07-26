# Developer documentation

This index is for contributors and maintainers who need to understand, change,
or validate the current cdh implementation. It routes each kind of development
work to its canonical guide and defines how long-lived documentation is owned
and maintained.

## Guides

- [Contributing](contributing.md) covers the shared development environment,
  repository structure, coding and validation workflow, and review rules.
- [Architecture](architecture.md) describes the current system context,
  component responsibilities, dependency direction, and the small set of
  execution scenarios needed to understand the design.
- [Contracts](contracts.md) records cross-module authority, schema, ownership,
  trust, replay, evidence, transfer, and process/lifecycle invariants.
- The [testing handbook](../../tests/README.md) owns test layers, cost
  authorization, acceptance selection, and test-resource discipline.

For product use, start from the [documentation index](../README.md). Return to
the [project overview](../../README.md) for installation and the shortest quick
start.

## Documentation governance

Tracked documentation describes the current product and development contract.
It is not a work log or a history of how the design was reached.

### Audience and ownership

Each document must serve one named audience and reading purpose. Assign each
fact one canonical owner and link to that owner instead of copying complete
field lists, option tables, examples, commands, or contract explanations into
another document.

Cross-audience copy may retain a one- or two-sentence summary or warning only
when the reader needs it for immediate operation, safety, data-loss prevention,
or a trust or support boundary. The summary must link to the complete owner and
must not expand its promise.

Keep test policy in the [testing handbook](../../tests/README.md) and
agent-specific automatic-context instructions in
[`AGENTS.md`](../../AGENTS.md). The [documentation index](../README.md) owns
cross-audience navigation rather than duplicating guide content.
Shared environment, coding, Git, and review guidance belongs in
[Contributing](contributing.md).

### Machine and behavioral authority

Production code, strict models, CLI help, executable examples, and behavioral
tests are the authorities for exact fields, defaults, accepted values, command
options, and behavior. Documentation explains how to use or preserve those
authorities; it must not become a parallel schema, option inventory, or test
oracle.

When a behavior change makes documentation stale, update or remove that copy in
the same change. Resolve disagreement against the current machine and
behavioral authorities instead of preserving obsolete prose for compatibility.

### Current rationale, not provenance

Retain rationale only when losing it would make a concrete incorrect change
likely. State the ownership, trust, dependency, or lifecycle error that the
rationale prevents, and keep it beside the current rule in
[Architecture](architecture.md) or [Contracts](contracts.md).

Do not copy plans, task status, branch names, commit hashes, review evidence,
rejected alternatives, or implementation chronology into tracked
documentation. Planning and review records remain outside the long-lived
documentation surface. Do not reconstruct historical decisions as an ADR
archive.

### Writing and maintenance

Use current-tense English, relative repository links, stable descriptive
headings, and directly runnable commands where commands add value. Remove
obsolete wording when its authority changes, and avoid speculative future
design, generated filler, and repeated examples.

There is no mechanical line target. Concision follows from a document's
audience, canonical ownership, durable value, and the rule to link instead of
copying.
