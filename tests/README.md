# Testing

The test suite is offline by default. `uv run pytest` may collect tests that describe network, Docker, GPU, or slow behavior, but those tests are skipped unless every matching cost authorization is present.

See the [contribution guide](../docs/dev/contributing.md) for environment setup, shared coding rules, and the general development workflow.

## Suite layout

- `tests/unit/` covers isolated business, validation, serialization, and trust boundary contracts.
- `tests/integration/` covers behavior that crosses CLI, rendering, subprocess, or filesystem boundaries without external services.
- `tests/smoke/` contains opt-in live provider and image checks.
- `tests/fixtures/` contains public configuration and local input fixtures.

Unit and integration tests are subdivided by execution owner:

- `host/` owns the supported native-host closure: package and root CLI, configuration, shared models and protocols, rendering, host orchestration, filesystem, Git, and Docker/Buildx adapters.
- `container/` owns helpers that execute only inside the project's Linux image.
- `distribution/` owns the canonical Linux sdist, archive, verifier, and byte-identity authority. Host-side wheel construction and ordinary package metadata/import behavior remain under `host/`.
- `support/` owns pytest authorization, the acceptance catalog, and other test-framework contracts.

Classify a test by the behavior it executes, not by every production module it imports or by Linux paths represented as data. Split a file only when it exercises more than one execution owner. Test helpers shared across owners live directly under `tests/` so imports and repository-root discovery do not depend on a test file's directory depth.

Keep tests at the narrowest layer that owns the behavior. Test current public, security, and execution contracts; do not preserve removed behavior with absence guards. Temporary development-only tests must be identified in a code comment and removed before the related change is complete.

Give parameterized cases concise, stable IDs when their values are large, binary, control-bearing, or otherwise unsuitable for display. Pytest exposes node IDs through process state and CI logs, so retain the full payload as test data without allowing it to become the generated case name. This keeps platform limits and diagnostic output independent from the boundary value being tested. The shared collection policy rejects a complete node ID longer than 4,096 characters before test execution and reports only the count and observed lengths, never the node ID content.

## Platform coverage

The complete default-offline suite runs on Linux for every supported Python minor. Required Windows validation runs the same `tests/unit/host` and `tests/integration/host` contract on Python 3.12, 3.13, and 3.14, followed by a wheel build and isolated-install smoke in every matrix cell. Windows selects those owner directories before collection, so Linux-only container and canonical distribution modules are not imported merely to be skipped. [The CI workflow](../.github/workflows/ci.yml) is the machine authority for the exact required matrix.

Place an operating-system-specific test at the narrowest unit or integration owner and guard it with a local `skipif` based on the native capability it requires. Operating-system selection is not an external-cost authorization, so do not add a Linux or Windows cost marker. Keep Linux-only container execution tests on Linux, and use native Windows tests for Win32 filesystem, DACL, descriptor-lock, Git-for-Windows, path, and process behavior. Mocked Docker and Buildx adapter tests prove only argument and error contracts; they do not prove Docker Desktop, named-pipe SSH forwarding, GPU use, or Windows-container execution.

Do not add a custom platform marker as a second platform inventory. Cross-platform host tests carry no platform label and run on every supported host. A file wholly dependent on one native platform may use a module-level built-in `skipif`; a mixed file guards only the narrow test that needs the unavailable capability, preferring capability detection over an operating-system name when practical. Custom markers classify tests or authorize external cost; they do not skip tests by themselves or prevent a module import during collection. Owner-directory selection remains the authority that keeps unsupported implementations outside a platform's import closure.

Run the affected selection on its native platform, for example:

```bash
uv run pytest tests/integration/host/test_windows_host_boundary.py
uv run pytest tests/unit/host tests/integration/host
```

Do not add hostile source-directory mutation races, stress loops, timing/fairness assertions, or extra platform combinations to strengthen the cooperative host source-read contract. Keep tests for independent container write, placement, and execution containment intact; those boundaries are not reduced by the host compatibility policy.

## Cost authorization

Cost-sensitive tests use strict pytest markers and matching command-line authorization:

| Marker | Authorization | External capability |
| --- | --- | --- |
| `network` | `--run-network` | Upstream or provider network access |
| `docker` | `--run-docker` | Local Docker daemon and image state |
| `gpu` | `--run-gpu` | Local GPU and driver |
| `slow` | `--run-slow` | Intentionally long execution |

`unit`, `smoke`, and `acceptance` classify tests; they do not authorize network, Docker, GPU, or slow execution.

A test with multiple cost markers requires every corresponding option. The options authorize marked tests; they do not add tests or make an unavailable external dependency successful.

Useful commands include:

```bash
uv run pytest
uv run pytest tests/unit tests/integration
uv run pytest tests/smoke/test_python_group_resolver_live.py \
  --run-network --run-docker
CDH_APPLICATION_ZERO_IMAGE=example/image:tag \
CDH_APPLICATION_ZERO_CONTEXT=/path/to/rendered-context \
  uv run pytest tests/smoke/test_application_acceptance_live.py \
  --acceptance-scenario py313-zero \
  --run-network --run-docker --run-gpu --run-slow
```

Canonical-lock tests should use the Docker-free matching-lock path unless they specifically own provider acquisition or resolution behavior. Live Python-group resolver tests require both `docker` and `network`, use the local/default Linux x86_64 Docker baseline, and must clean only their uniquely owned containers or images. Do not assume Docker Desktop, TLS, SSH, or remote-daemon compatibility from the local baseline.

## Acceptance scenarios

`tests/acceptance_scenarios.py` is the machine-consumed authority for scenario identity, public config input, Python profile, capabilities, cost, scenario classification, and required image/context inputs. The referenced TOML files remain the public configuration authority for exact nodes, versions, hooks, and downloads. Test modules own assertions about the behavior they observe; do not copy either authority into prose.

Durable release runs select one or more catalog IDs with the repeatable `--acceptance-scenario ID` option. Supply the image and rendered-context environment inputs for every selected scenario, then authorize all costs declared by each selected typed catalog scenario. Missing inputs for a selected release scenario are failures, not skips. Moving-input scenarios are non-blocking canaries and must be reported separately from release acceptance.

Render each selected context once, build its image once, and reuse both across all applicable inspection, CPU, startup, CLI, and GPU probes. Every durable release image must pass its real GPU functional probe; the complete selected release run therefore authorizes network, Docker, GPU, and slow costs. Only the functional GPU probe needs GPU device access. Context inspection, image metadata, CLI, PID-topology, and other auxiliary containers do not acquire a GPU merely because they share the same release run. Clean-cache rebuilds and the complete GPU-required matrix are reserved for changes that own those risks or for a cumulative release gate.

Lifecycle changes reuse one current image produced by the formal renderer; do not create a second handwritten Dockerfile for lifecycle tests. Set `CDH_LIFECYCLE_CONTEXT` to that rendered context and `CDH_LIFECYCLE_IMAGE` to the image built from it, then run `tests/smoke/test_lifecycle_shutdown_live.py` with `--run-docker --run-slow`. The suite bind-mounts deterministic programs, runtime config, and hooks into that image to cover Tini/cdh topology, first/repeated signals, finite and disabled cdh deadlines, active-hook cancellation, paired background-service shutdown, natural exit, and adopted-orphan reap. Separate real process-control integration proves cdh-managed child reap inside the ownership window; the formal force test also holds cdh during interpreter exit and observes that its direct application child is already absent from `/proc`. Container exit alone is not that evidence. Graceful service shutdown and Tini orphan-zombie reap also require their own observed markers. The six-image release matrix repeats only lightweight PID topology, default SIGTERM, and ordinary application shutdown checks.

## Change selection and cleanup

Start with focused tests for the changed owner, then expand to adjacent integration coverage. Run the full offline suite before handoff. Add live or high-cost checks only when the change affects their provider, image, runtime, or hardware boundary.

Use dedicated tags, containers, contexts, and logs for live image work. Record the exact image ID and preserve relevant evidence until review completes. Remove only resources created by the run; do not delete unrelated images, volumes, or caches. Never place passwords, private keys, tokens, or other credentials in fixtures or captured logs.
