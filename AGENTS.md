# Repository Agent Instructions

## Mandatory Shared Authorities

- Before any project work, read [`docs/dev/contributing.md`](docs/dev/contributing.md) completely and follow its environment, repository structure, coding, testing-workflow, Git, and review rules.
- Before selecting, adding, changing, or running tests, read [`tests/README.md`](tests/README.md) completely and follow its test-layer, cost-authorization, acceptance, validation-selection, and resource-discipline rules.
- Before changing tracked documentation, read [`docs/dev/README.md`](docs/dev/README.md) completely and follow its audience, canonical-ownership, current-only, and maintenance rules.

## External References

### ComfyUI

- [ComfyUI](https://github.com/Comfy-Org/ComfyUI)
- [comfy-cli](https://github.com/Comfy-Org/comfy-cli)
- [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
- [Comfy Registry](https://registry.comfy.org/)

## Agent-Specific Working Rules

### General
- Project work planning and progress documents live under `docs/workdesk/`. That path is intentionally gitignored and must not be committed.
- Each workdesk uses the canonical phase directories `01-discussion/`, `02-plan/`, and `03-progress/`. Create a phase directory when that phase begins; empty phase directories are not required.
  - `01-discussion/`: discussion evidence, open questions, and decisions.
  - `02-plan/`: accepted implementation plans.
  - `03-progress/`: execution status, validation evidence, and handoff records.
- Before using or modifying an existing workdesk, inspect its current directory and file structure and the relevant neighboring documents once for the task. Preserve the canonical phase ownership; do not create a new file until its intended role and location are clear.
- Keep work planning details in `docs/workdesk/`; do not mention milestones, task plans, or implementation work status in docs, examples, code comments, or user-facing copy.
- Within each worktree, handle only one active plan at a time, and within that plan, work on only one task at a time.
- Load task-relevant skills when the current agent runtime provides them.
- Sub-agents may take a long time to complete. After delegation, patiently wait for completion until the sub-agent has clearly stalled or terminated. While waiting for sub-agents, do not interrupt or require immediate report, and do not perform unnecessary parallel tasks.
- If genuine browser verification is required, use the preferred tool if specified by the current agent runtime; if sub-agent feature is available, delegate the browser verification task to a new sub-agent.

### Codex-Specific
- For Codex sub-agents delegation: prefer using model `gpt-5.6-luna` with `max` reasoning effort.
- Run the following commands with elevated execution:
  - Docker CLI commands, such as `docker build`.
  - Commands that may access a local GPU, such as `nvidia-smi`.
  - Commands that write to or update Git state, such as `git commit`.
- Preferred order for browser verification tools: Chrome plugin [@chrome](plugin://chrome@openai-bundled) > In-App Browser plugin [@Browser](plugin://browser@openai-bundled) > `chrome-devtools` MCP server > `playwright-cli`
