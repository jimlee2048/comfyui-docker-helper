# Testing

The test suite is offline by default. `uv run pytest` may collect tests that
describe network, Docker, GPU, or slow behavior, but those tests are skipped
unless every matching cost authorization is present.

## Suite layout

- `tests/unit/` covers isolated business, validation, serialization, and trust
  boundary contracts.
- `tests/integration/` covers behavior that crosses CLI, rendering, subprocess,
  or filesystem boundaries without external services.
- `tests/smoke/` contains opt-in live provider and image checks.
- `tests/fixtures/` contains public configuration and local input fixtures.

Keep tests at the narrowest layer that owns the behavior. Test current public,
security, and execution contracts; do not preserve removed behavior with
absence guards. Temporary development-only tests must be identified in a code
comment and removed before the related change is complete.

## Cost authorization

Cost-sensitive tests use strict pytest markers and matching command-line
authorization:

| Marker | Authorization | External capability |
| --- | --- | --- |
| `network` | `--run-network` | Upstream or provider network access |
| `docker` | `--run-docker` | Local Docker daemon and image state |
| `gpu` | `--run-gpu` | Local GPU and driver |
| `slow` | `--run-slow` | Intentionally long execution |

A test with multiple cost markers requires every corresponding option. The
options authorize marked tests; they do not add tests or make an unavailable
external dependency successful.

Useful commands include:

```bash
uv run pytest
uv run pytest tests/unit tests/integration
uv run pytest tests/smoke/test_python_group_resolver_live.py --run-network
uv run pytest tests/smoke/test_custom_node_image_live.py --run-docker --run-slow
```

The ordinary quality boundary is `scripts/run-quality-gates.sh <python>`. It
runs Ruff, offline unit and integration tests, package construction, isolated
wheel/CLI verification, and release-source projection. It does not authorize
network, Docker, GPU, or slow tests.

## Acceptance scenarios

`tests/acceptance_scenarios.py` is the machine-consumed authority for scenario
identity, public config input, Python profile, capabilities, cost, release or
canary classification, and required image/context inputs. The referenced TOML
files remain the public configuration authority for exact nodes, versions,
hooks, and downloads. Test modules own assertions about the behavior they
observe; do not copy either authority into prose.

Durable release runs select one or more catalog IDs with the repeatable
`--acceptance-scenario ID` option. Supply the selected scenario's image and
rendered-context environment inputs, then authorize all costs required by the
test module. Missing inputs for a selected release scenario are failures, not
skips. Moving-input scenarios are non-blocking canaries and must be reported
separately from release acceptance.

Render each selected context once, build its image once, and reuse both across
all applicable inspection, CPU, startup, CLI, and GPU probes. Clean-cache
rebuilds and complete GPU matrices are reserved for changes that own those
risks or for a cumulative release gate.

## Change selection and cleanup

Start with focused tests for the changed owner, then expand to adjacent
integration coverage. Run the full offline suite before handoff. Add live or
high-cost checks only when the change affects their provider, image, runtime,
or hardware boundary.

Use dedicated tags, containers, contexts, and logs for live image work. Record
the exact image ID and preserve relevant evidence until review completes.
Remove only resources created by the run; do not delete unrelated images,
volumes, or caches. Never place passwords, private keys, tokens, or other
credentials in fixtures or captured logs.
