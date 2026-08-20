# Contributing

This guide covers the shared development workflow for `comfyui-docker-helper`. Read the [architecture guide](architecture.md) before changing component responsibilities and the [contracts guide](contracts.md) before changing an authority, trust, replay, transfer, or process boundary. The [testing handbook](../../tests/README.md) is the canonical guide for test layers, cost authorization, acceptance scenarios, and live-resource handling.

## Development environment

Use [uv](https://docs.astral.sh/uv/) for Python installation, dependency management, command execution, and packaging. The [project configuration](../../pyproject.toml) owns the supported Python envelope and tool configuration; the [lockfile](../../uv.lock) owns the exact resolved development environment.

From the repository root, create or refresh the locked environment:

```bash
uv sync --locked
```

Confirm the local entry point and the minimal public configuration:

```bash
uv run cdh --help
uv run cdh host validate -f examples/minimal.toml
```

When using Python language features, keep them within the `requires-python` range declared in the project configuration.

## Platform compatibility

The package's root CLI, configuration, shared services, rendering, and `cdh host *` workflows support the declared Python minors on native Windows and Linux hosts. `cdh container *` executes only inside the project's Linux image, although its command group must remain importable and its help must remain usable on other hosts. Windows host support means a Windows operator can drive a Linux-container Docker endpoint; it does not add a Windows-container execution contract.

Keep Linux-only container implementations outside the host import closure. Put platform-specific imports behind the narrow owner that needs them, and preserve equivalent behavior rather than assuming that POSIX descriptors, modes, signals, shell paths, or environment conventions exist on Windows. Host source reads follow the cooperative-input contract, while private state and container write/execute boundaries retain separate stronger ownership rules; read the [cross-module contracts](contracts.md#host-local-filesystem-boundaries) before changing either boundary.

Filesystem, Git, Docker, and process behavior that branches by operating system needs focused native-platform coverage. Do not infer Windows behavior from a mocked POSIX test or infer Docker Desktop end-to-end support from adapter tests. The [testing handbook](../../tests/README.md#platform-coverage) owns platform test placement and the uniform supported-minor Windows host matrix.

### Optional workflow diagnostics

uv remains the sole authority for Python installation, environments, dependency management, and package execution. Maintainers changing GitHub workflow files may optionally install the lightweight workflow diagnostics declared in the root `mise.toml`:

```bash
mise install
mise exec -- actionlint \
  -ignore 'unexpected key "queue" for "concurrency" section' \
  .github/workflows/*.yml
```

The narrow ignore covers actionlint 1.7.12's stale schema for GitHub's supported `concurrency.queue` field; remove it after the installed actionlint release supports that field. The mise environment also makes ShellCheck available to actionlint for embedded shell diagnostics. These tools are workflow-maintenance conveniences, not prerequisites for ordinary contributions and not an alternative Python toolchain.

## Repository structure

The project uses a `src` layout:

- `src/comfyui_docker_helper/config/` owns strict configuration, validation, merge, and planning models.
- `src/comfyui_docker_helper/host/` owns commands and orchestration that run on the host.
- `src/comfyui_docker_helper/rendering/` materializes Docker build contexts.
- `src/comfyui_docker_helper/container/` owns build-container and runtime helpers.
- `src/comfyui_docker_helper/resources/` contains package-owned implementation inputs.
- `tests/` contains unit and integration tests subdivided by host, Linux-container, distribution, and test-support ownership, plus smoke tests, shared support, and fixtures.
- `tools/ci/` contains directly runnable, read-only package-build validators.
- `examples/` contains the minimal and comprehensive public configurations.
- `docs/user/` and `docs/dev/` contain user and developer documentation.

See [Architecture](architecture.md) for component responsibilities and allowed dependency direction. Place changes at the narrowest owner instead of creating a second authority in a nearby module.

## Development workflow

Keep each change focused on its current behavior owner. Start validation with the closest unit or integration tests, expand to adjacent coverage, and run the complete default-offline suite before handoff. Add network, Docker, GPU, or slow tests only when the changed boundary requires them; follow the [testing handbook](../../tests/README.md) for exact authorization and resource rules.

Run the configured formatting and lint checks:

```bash
uv run ruff format --check .
uv run ruff check .
```

Run focused tests while developing, then the complete offline suite:

```bash
uv run pytest tests/unit
uv run pytest
```

Run platform-specific tests on the platform whose native behavior they claim. Every supported Windows Python minor runs the same host/shared selection:

```bash
uv run pytest tests/unit/host tests/integration/host
```

The exact required matrix lives in [the CI workflow](../../.github/workflows/ci.yml). Linux remains authoritative for the complete default-offline suite, Linux-image container helpers, and canonical distribution qualification; do not collect those owners on Windows merely to skip them.

Validate affected examples and CLI paths directly. At minimum, configuration changes should keep the minimal example valid:

```bash
uv run cdh host validate -f examples/minimal.toml
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

### CLI presentation

The root CLI owns one immutable detail setting for the complete invocation. `-q/--quiet` suppresses cdh-owned informational progress and summaries, ordinary mode shows the minimum useful human state, `-v/--verbose` adds safe operational detail, and `-vv` adds safe debug detail. Quiet and verbose are mutually exclusive, and these root-only options must precede the `host` or `container` group. Do not duplicate detail, color, or progress options on group or leaf commands.

Detail filtering applies only to optional cdh-owned presentation. Help, version, explicit dry-run or machine-readable data, runtime status, restart results, and followed bytes remain command results. Warnings, expected diagnostics, and controlled failures remain visible under quiet. Stdout owns successful command results and explicit data; stderr owns cdh progress, lifecycle state, warnings, and expected diagnostics. Preserve that ownership when stdout and stderr are redirected independently.

Detect capabilities independently from each concrete stdout and stderr destination. A non-terminal or incapable destination receives flushed plain text without ANSI or cursor-rewriting controls. A capable one-shot terminal destination may use Rich layout, color, and transient Live/Progress; durable Runtime output stays plain even on a terminal. `NO_COLOR` disables cdh-owned color without changing semantic content or stream ownership. Exact color, glyph, spacing, wrapping, wording, and refresh cadence are presentation details, not contracts.

Host validate, render, and build preparation use the host operator presenter. Validate may summarize ordered configuration layers on an interactive stdout, while verbose makes the same semantic result observable on a plain destination. Render and build preparation expose truthful major phases on stderr so a non-interactive invocation does not appear stalled, then put the successful context or build result on stdout. Interactive preparation may be transient; it must stop before the first library-yielded BuildKit line. The Host writes each yielded message to stdout without a cdh prefix, style, or detail filter and normalizes its ending to one newline.

Image-internal one-shot helpers use plain, append-only execution events on stderr while preserving their child processes' inherited streams. A directly interactive file download may use Rich Progress on stderr; its non-interactive form uses durable state and bounded progress snapshots, and quiet suppresses only cdh-owned download presentation. Runtime lifecycle output is a durable plain stderr log. Main lifecycle facts render synchronously as complete lines, while genuine background producers use the Runtime-owned bounded/coalesced delivery boundary inside the logging broker.

Workflow, progress, and lifecycle producers that cross an event boundary emit immutable typed facts through narrow injected sinks; the owning event presenter selects detail, stream, terminal form, and wording. Event fields must be validated, role-owned, and safe to display. Explicit results, direct diagnostics, and Typer output retain their existing owners and do not need an event wrapper. Keep human diagnostics concise and actionable; do not reconstruct display content from raw configuration, arbitrary exception text, command arguments, URLs, provider payloads, or exception chains. Render approved dynamic text control-safely, and never expose resolved Secret values, Secret source locators, or other unapproved sensitive content.

Inherited external and child output retains the stream and trust boundary of its producer. cdh does not prefix, restyle, parse, redact, or make terminal-safety promises for ComfyUI, hooks, SSH, or another inherited child stream. BuildKit is the distinct library-yielded line stream described above, not a byte-preserving inherited stream. Captured provider or protocol output remains adapter-owned and may instead be converted into bounded controlled diagnostics.

Test presentation at its narrow owner through semantic events, detail admission, stdout/stderr ownership, independent terminal capabilities, control safety, and idempotent success/failure/interruption teardown. Use fake clocks or schedulers for progress cadence and bounded delivery. Keep at least one representative real stream or terminal boundary where integration behavior matters, but avoid snapshot suites or matrices that freeze decoration, spacing, wrapping, color, glyphs, progress frames, or complete wording.

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
