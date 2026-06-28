# Repository Guidelines

## About this Project

`comfyui-docker-helper`: A Python CLI package helps to build and run ComfyUI in docker container.


## Project Structure

This project uses a src/ layout.

- `src/comfyui_docker_helper/`: The importable Python CLI package.
    - `config/`: config models, validation, merge, and render plans.
    - `rendering/`: materializes Docker build contexts.
    - `host/`: commands executed on the host machine.
    - `container/`: helpers executed inside Docker build containers.
    - `templates/`: stores template files used by the internal implementation.
- `tests/`: Test suites and fixtures.
    - `tests/unit/`: unit tests .
    - `tests/integration/`: integration tests.
    - `tests/smoke/`: smoke tests.
    - `tests/fixture/<set>`: test fixture sets.
- `examples/`: User-facing config examples.


## Development Toolchain

Use `uv` for Python execution, dependency management, and packaging:
- `uv sync --locked`: install the locked development environment.
- `uv build`: build wheel and source distribution artifacts.
- `uv run cdh --help`: verify the local CLI entry point.
- `uv run cdh host validate -f examples/minimal.toml`: validate a sample config.

Use `ruff` for formatting and lint checks:
- `uv run ruff format --check .`: check formatting without rewriting files.
- `uv run ruff check .`: run lint checks.

Use `pytest` for testing:
- `uv run pytest`: run the full pytest suite.


## Coding Guidelines

### General
- Use spaces for indentation, LF line endings, double quotes.
- Use English for identifiers, code comments, commit messages, and technical docs.
- Keep comments simple and useful. Explain intent or non-obvious constraints, not obvious code.
- Keep CLI-facing errors user-readable.

### Python
- Keep modules, functions, variables, and pytest tests in `snake_case`; use `PascalCase` for classes and Pydantic models.
- Use Pydantic models for structured configuration and validation boundaries.
- Keep Pydantic models strictly typed; prefer precise field types, `Literal`, and discriminated unions over broad `dict`, `object`, or `Any`.
- Prefer `pathlib.Path` for filesystem code.
- When using Python language features, check the requires-python field in pyproject.toml for the current minimum supported runtime.


## Testing Guidelines

- Name test files `test_*.py` and place focused unit coverage in `tests/unit/`.
- Pytest is configured with strict markers. Mark expensive or environment-dependent tests with the existing markers: `docker`, `network`, `gpu`, `slow`, or `smoke`.
- Use `tests/integration/` when behavior crosses CLI, rendering, subprocess, or filesystem boundaries.
- Test the current behavior contract; avoid guard tests that only assert deprecated legacy behavior is absent.
- Mark temporary development-only tests with a code comment, and remove them before the related work is complete.
- For quicker local checks, run targeted paths such as `uv run pytest tests/unit` before the full suite.


## Commit & Pull Request Guidelines

- Write concise commit messages in English, following [Conventional Commits specification](https://www.conventionalcommits.org/en/v1.0.0/#specification).
- Pull requests should describe behavior changes, list validation commands run, link related issues when available, and include screenshots or logs only when CLI output, Docker rendering, or diagnostics changed.


## Agent-Specific Working Rules

### General
- Project work planning and progress documents live under `docs/workdesk/`. That path is intentionally gitignored and must not be committed.
- Keep work planning details in `docs/workdesk/`; do not mention milestones, task plans, or implementation work status in docs, examples, code comments, or user-facing copy.
- Work on one active plan at a time, and one plan task inside it at a time.
- Load task-relevant skills when the current agent runtime provides them.
- For real browser verification, use the browser verification tool selected by the current agent runtime when it declares a preference. Delegate browser verification to a sub-agent when sub-agents are available.
- Sub-agents may take a long time to complete. After delegation, patiently wait for completion until the sub-agent has clearly stalled or terminated. While waiting for sub-agents, do not interrupt or require immediate report, do not perform unnecessary parallel tasks.

### Codex-Specific
- Run the following commands with elevated execution permissions:
    - Docker CLI commands, such as `docker build`.
    - Commands that may access a local GPU, such as `nvidia-smi`.
    - Commands that write to or update Git state, such as `git commit`.
- Browser verification tool selection: prefer Chrome plugin [@chrome](plugin://chrome@openai-bundled), then In-App Browser plugin [@Browser](plugin://browser@openai-bundled).
- When delegating sub-agents:
    - use reasoning effort `medium` for implementation.
    - use reasoning effort `xhigh` for planning or review.
    - use reasoning effort `low` for browser verification.
