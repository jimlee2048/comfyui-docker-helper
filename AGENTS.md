# Repository Agent Instructions

## Mandatory Shared Authorities

- Before any project work, read
  [`docs/dev/contributing.md`](docs/dev/contributing.md) completely and follow
  its environment, repository structure, coding, testing-workflow, Git, and
  review rules.
- Before selecting, adding, changing, or running tests, read
  [`tests/README.md`](tests/README.md) completely and follow its test-layer,
  cost-authorization, acceptance, validation-selection, and
  resource-discipline rules.
- Before changing tracked documentation, read
  [`docs/dev/README.md`](docs/dev/README.md) completely and follow its audience,
  canonical-ownership, current-only, and maintenance rules.

## Agent-Specific Working Rules

### General
- Project work planning and progress documents live under `docs/workdesk/`. That path is intentionally gitignored and must not be committed.
- Keep work planning details in `docs/workdesk/`; do not mention milestones, task plans, or implementation work status in docs, examples, code comments, or user-facing copy.
- Work on one active plan at a time, and one plan task inside it at a time.
- Use English for normative tracked technical documentation. Maintained
  localized documentation may use its target language, but it must link its
  normative English source, preserve the same behavior and safety boundaries,
  and be assessed whenever either member of the pair changes.
- Load task-relevant skills when the current agent runtime provides them.
- For real browser verification, use the browser verification tool selected by
  the current agent runtime when it declares a preference. When using Codex,
  prefer Chrome plugin [@chrome](plugin://chrome@openai-bundled), then In-App
  Browser plugin [@Browser](plugin://browser@openai-bundled). Delegate browser
  verification to a sub-agent when sub-agents are available.
- Sub-agents may take a long time to complete. After delegation, patiently wait
  for completion until the sub-agent has clearly stalled or terminated. While
  waiting for sub-agents, do not interrupt or require immediate report, and do
  not perform unnecessary parallel tasks.

### Codex-Specific
- Run the following commands with elevated execution permissions:
    - Docker CLI commands, such as `docker build`.
    - Commands that may access a local GPU, such as `nvidia-smi`.
    - Commands that write to or update Git state, such as `git commit`.
