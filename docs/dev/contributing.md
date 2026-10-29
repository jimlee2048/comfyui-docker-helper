# Contributing

This guide covers the shared development workflow for `comfyui-docker-helper`. Read the [architecture guide](architecture.md) before changing component responsibilities and the [contracts guide](contracts.md) before changing an authority, trust, replay, transfer, or process boundary. The [testing handbook](../../tests/README.md) is the canonical guide for test layers, cost authorization, acceptance scenarios, and live-resource handling.

## Development environment

Use [uv](https://docs.astral.sh/uv/) for Python installation, dependency management, command execution, and packaging. The [project configuration](../../pyproject.toml) owns the supported Python envelope and tool configuration; the [lockfile](../../uv.lock) owns the resolved dependency versions, not the Python interpreter or uv executable.

Local development may use any supported Python version. To select a version explicitly, pass `--python` to `uv sync` or create an ignored local `.python-version` with `uv python pin`. CI selects its Python versions independently.

From the repository root, create or refresh the locked environment:

```bash
uv sync --locked
```

Confirm the local entry point and the minimal public configuration:

```bash
uv run --locked cdh --help
uv run --locked cdh host validate -f examples/minimal.toml
```

When using Python language features, keep them within the `requires-python` range declared in the project configuration.

## Platform compatibility

The package's root CLI, configuration, shared services, rendering, and `cdh host *` workflows support the declared Python minors on native Linux x86_64/ARM64, Windows x86_64, and macOS ARM64 hosts. `cdh container *` executes only inside the project's Linux image, although its command group must remain importable and its help must remain usable on other hosts. Windows host support means a Windows operator can drive a Linux-container Docker endpoint; it does not add a Windows-container execution contract.

Keep Linux-only container implementations outside the host import closure. Put platform-specific imports behind the narrow owner that needs them, and preserve equivalent behavior rather than assuming that POSIX descriptors, modes, signals, shell paths, or environment conventions exist on Windows. Host source reads follow the cooperative-input contract, while private state and container write/execute boundaries retain separate stronger ownership rules; read the [cross-module contracts](contracts.md#host-local-filesystem-boundaries) before changing either boundary.

Filesystem, Git, Docker, and process behavior that branches by operating system needs focused native-platform coverage. Do not infer Windows behavior from a mocked POSIX test or infer Docker Desktop end-to-end support from adapter tests. The [testing handbook](../../tests/README.md#platform-coverage) owns platform test placement and the supported-minor native platform matrix.

### Optional workflow diagnostics

Maintainers changing GitHub workflows may optionally install [actionlint](https://github.com/rhysd/actionlint) with [ShellCheck](https://github.com/koalaman/shellcheck) for local workflow and embedded shell diagnostics; follow their upstream installation and usage instructions.

## Repository structure

The project uses a `src` layout. See the [architecture responsibility map](architecture.md#component-responsibilities) to choose the production-code owner.

| Directory | Contents |
| --- | --- |
| `src/comfyui_docker_helper/` | Package implementation and owned resources |
| `tests/` | Unit, integration and opt-in smoke tests; placement follows the testing handbook |
| `tools/ci/` | Read-only package-build validators |
| `examples/` | Minimal and comprehensive configurations, hook examples and local fixtures |
| `docs/user/`, `docs/dev/` | Operator and maintainer guides |

Place changes at the narrowest owner instead of creating a second authority in a nearby module.

## Development workflow

Keep each change focused on its current behavior owner. Select validation and its completion criteria using the [testing handbook](../../tests/README.md#change-selection-and-cleanup), including its exception for documentation, comments, and agent instructions that do not affect execution. The handbook owns test scope, cost authorization, and resource rules; use the commands below as applicable to that selection.

Use `uv run --locked` for verification so stale dependency metadata fails instead of updating `uv.lock` during a check. The environment is still synchronized as needed. Manage dependency changes with `uv add` or `uv remove`; after manual dependency edits, run `uv lock`. To deliberately upgrade one dependency, use `uv lock --upgrade-package PACKAGE`. Review and commit the affected dependency declarations and lockfile together before handing off the change.

Run the configured formatting and lint checks:

```bash
uv run --locked ruff format --check .
uv run --locked ruff check .
```

For changes requiring test execution under the handbook, run focused tests while developing, then the complete offline suite:

```bash
uv run --locked pytest tests/unit
uv run --locked pytest
```

Run platform-specific tests on the platform whose native behavior they claim. The [testing handbook](../../tests/README.md#platform-coverage) owns platform coverage and the [CI workflow](../../.github/workflows/ci.yml) selects the exact matrix. Host-only validation on supported Windows and ARM64 hosts uses:

```bash
uv run --locked pytest tests/unit/host tests/integration/host
```

Validate affected examples and CLI paths directly. At minimum, configuration changes should keep the minimal example valid:

```bash
uv run --locked cdh host validate -f examples/minimal.toml
```

Build the source distribution and wheel through the declared PyPA backend:

```bash
uv build \
  --no-sources \
  --force-pep517 \
  --no-create-gitignore
```

The required validation depth depends on the changed risk. Do not treat a high-cost run as a substitute for focused contract tests, and do not run external-cost gates solely because they exist.

## Code guidelines

- Use spaces for indentation, LF line endings, and double quotes.
- Use English for identifiers, code comments, and commit messages.
- Use `snake_case` for modules, functions, variables, and pytest tests, and `PascalCase` for classes and Pydantic models.
- Name test files `test_*.py`; keep focused unit coverage in `tests/unit/` and place boundary coverage at the narrowest layer defined by the testing handbook.
- Keep imports at module scope unless preventing a real circular dependency requires otherwise.
- Catch specific exceptions and avoid unnecessary `try`/`except` blocks.
- Use `pathlib.Path` for filesystem code.
- Use strict, precisely typed Pydantic models at structured configuration, validation, and serialization boundaries. Prefer precise field types, `Literal`, and discriminated or explicit unions over broad `dict`, `object`, or `Any`.
- Use explicit typed fields for parent control flow instead of probing child objects with arbitrary `getattr(..., default)` calls.
- Pass only the narrow data a module owns; avoid broad context objects unless the callee owns that responsibility.
- Remove dead fallbacks, migration paths, unused options, debug prints, and unreachable code when a newer authority replaces them.

Comments should explain intent or a non-obvious constraint, not restate the code. Tests should protect the current behavior contract; follow the [testing handbook](../../tests/README.md) when placing or removing coverage.

### Generated Dockerfiles

The rendered Dockerfile is executable build output first, but it is also an operator-inspectable artifact that can be built directly. Preserve correctness, trust boundaries, execution order, and cache behavior ahead of brevity or visual polish while keeping the effective build flow structurally legible. Group distinct responsibilities into semantic sections, format comparable instructions consistently, and keep mounts, credentials, and build-only environment policy adjacent to the command that consumes them. Do not combine unrelated operations, introduce global state, or hide trust-sensitive behavior behind extra indirection merely to reduce line or layer count.

Inline programs, heredocs, generated helper scripts, and template engines are not categorically forbidden. Introduce an additional representation layer only for a concrete ownership, reuse, or escaping benefit, and keep the effective command, scope, and trust boundary reviewable in the rendered result. Exact comments, whitespace, wrapping, and line count remain implementation details unless explicitly promoted to a compatibility contract. Follow the [testing handbook](../../tests/README.md#change-selection-and-cleanup) for renderer-test boundaries.

### CLI presentation

Before changing output, read [CLI observation and stream ownership](contracts.md#cli-observation-and-stream-ownership). It owns detail filtering, stream destinations, terminal behavior and the boundary between cdh presentation and external output.

- Emit typed, safe facts through the owning event sink when a workflow crosses a presentation boundary; keep wording and terminal layout in its presenter.
- Keep explicit command results and direct diagnostics with their existing owners. Do not wrap every output in an event.
- Write concise, actionable diagnostics. Never construct them from arbitrary exceptions, URLs, arguments, provider payloads or resolved secrets.
- Test semantic events, detail admission, stream ownership, terminal capabilities and teardown at the narrow owner. Use fake clocks for cadence and retain a representative real-stream integration where needed. Do not freeze decoration, progress frames or complete wording in snapshots.

## Documentation

Update or remove affected documentation in the same change as behavior. Follow the canonical ownership, current-state, and bounded-duplication rules in the [developer documentation index](README.md). Keep exact fields, options, and accepted values in their machine authorities instead of creating parallel prose inventories. When changing one member of a maintained English/localized pair, assess and update its counterpart in the same change when affected.

## Commits and pull requests

Write concise English commit subjects using [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

A pull request should:

- describe the behavior change and its user or maintainer impact;
- list the validation commands actually run;
- link related issues when available; and
- include screenshots or logs only when CLI output, Docker rendering, or diagnostics changed.

Keep review material relevant to the current change. Do not turn source comments, documentation, or pull requests into implementation-progress logs.
