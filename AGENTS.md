# Repository Agent Instructions

## Mandatory Shared Authorities

Read each authority completely when it first applies to the current task. Reuse that reading while its contents remain available in context and the file is unchanged.

- Before any project work, read [`docs/dev/contributing.md`](docs/dev/contributing.md) completely and follow its environment, repository structure, coding, testing-workflow, Git, and review rules.
- Before selecting, adding, changing, or running tests, read [`tests/README.md`](tests/README.md) completely and follow its test-layer, cost-authorization, acceptance, validation-selection, and resource-discipline rules.
- Before changing tracked documentation, read [`docs/dev/README.md`](docs/dev/README.md) completely and follow its audience, canonical-ownership, current-only, and maintenance rules.

## External References

### ComfyUI

Consult these references when the task involves the corresponding upstream integration.

- [ComfyUI](https://github.com/Comfy-Org/ComfyUI)
- [comfy-cli](https://github.com/Comfy-Org/comfy-cli)
- [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
- [Comfy Registry](https://registry.comfy.org/)

## Agent-Specific Working Rules

### General

- Use a workdesk for complex work that needs durable discussion, planning, or handoff records. Simple questions, read-only reviews, and small local fixes do not require one unless the user requests it. Project work planning and progress documents live under `docs/workdesk/`; that path is intentionally gitignored and must not be committed.
- Each workdesk uses the canonical phase directories `01-discussion/`, `02-plan/`, and `03-progress/`. Create a phase directory when that phase begins; empty phase directories are not required.
  - `01-discussion/`: discussion evidence, open questions, and decisions.
  - `02-plan/`: accepted implementation plans.
  - `03-progress/`: execution status, validation evidence, and handoff records.
- Workdesk structure does not add a plan-approval step. Follow the user's requested discussion or approval boundaries and proceed within existing authorization.
- Before using or modifying an existing workdesk, inspect its current directory and file structure and the relevant neighboring documents once for the task. Preserve the canonical phase ownership; do not create a new file until its intended role and location are clear.
- Keep work planning details in `docs/workdesk/`; do not mention milestones, task plans, or implementation work status in docs, examples, code comments, or user-facing copy.
- Within each worktree, handle only one active plan and one implementation task at a time. Independent investigation, review, or verification subtasks supporting that implementation task may run in parallel when useful and supported by the runtime. Do not advance other plan tasks concurrently or allow overlapping edits to the same files.
- Load task-relevant skills when the current agent runtime provides them.
- Give sub-agents time to complete without demanding immediate reports or duplicating their work. Continue independent work within the current task when available; otherwise wait. Adjust or stop delegated work when the user cancels or changes scope, or when there is evidence of incorrect work, a stall, or termination.
- If genuine browser interaction is required for verification, use the preferred available tool specified by the current runtime and delegate to a new sub-agent when that capability is available.

### Codex-Specific

- Run the following commands with elevated execution:
  - Docker CLI commands, such as `docker build`.
  - Commands that may access a local GPU, such as `nvidia-smi`.
  - Commands that write to or update Git state, such as `git commit`.
- Among available browser verification tools, prefer Chrome plugin [@chrome](plugin://chrome@openai-bundled) > In-App Browser plugin [@Browser](plugin://browser@openai-bundled) > `chrome-devtools` MCP server > `playwright-cli`. This preference does not require installing unavailable tools.
