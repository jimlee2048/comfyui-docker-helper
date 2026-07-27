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
- `tests/` contains unit, integration, smoke, acceptance support, and fixtures.
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
- Keep CLI errors, warnings, and information short, user-readable, and actionable; remove noisy or misleading messages instead of adding logging.
- Remove dead fallbacks, migration paths, unused options, debug prints, and unreachable code when a newer authority replaces them.

Comments should explain intent or a non-obvious constraint, not restate the code. Tests should protect the current behavior contract; follow the [testing handbook](../../tests/README.md) when placing or removing coverage.

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
