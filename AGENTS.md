# AGENTS.md

## Project Working Rules

### 1. General Principles

- Preserve the existing project architecture unless the user explicitly requests an architectural change.
- Prefer small, focused changes over broad rewrites.
- Do not refactor working code only for stylistic reasons.
- Explain significant architectural changes before implementing them.
- Prefer official documentation when verifying framework APIs, configuration, or library behavior.
- Never expose, print, copy, or commit secrets.

---

## 2. Core Agent Architecture

The following are considered core project logic:

- LangGraph workflow
- graph nodes and edges
- State design
- routing strategy
- business rules
- tool boundaries
- human-in-the-loop behavior
- PostgreSQL persistence behavior

For core logic:

- Do not redesign the graph without explicit user approval.
- Do not add, remove, merge, or significantly reorganize nodes without explaining why.
- Do not silently change business rules.
- Do not replace deterministic business rules with LLM decisions unless explicitly requested.
- Preserve existing human-in-the-loop behavior unless a change is requested.
- Preserve persistence behavior unless a change is requested.
- When the user provides the architecture, implement that architecture rather than replacing it with a different design.

When reviewing user-written core logic:

1. Review the existing implementation first.
2. Identify bugs, edge cases, maintainability issues, and test gaps.
3. Explain significant issues before broad structural changes.
4. Prefer fixing the existing implementation over rewriting it from scratch.

---

## 3. Testing Rules

Use `pytest` for Python tests unless the project already uses another test framework.

All meaningful tests should be persistent project tests.

- Put tests under `tests/`.
- Prefer modifying existing tests when appropriate.
- Create new test files only when they provide long-term test coverage.
- Do not create disposable debugging scripts such as:
  - `temp_test.py`
  - `test_tmp.py`
  - `debug.py`
  - `debug_test.py`
  - `scratch.py`
  - similar temporary Python files

Do not repeatedly create and delete temporary scripts to work around execution problems.

### Environment-specific failures

If a test cannot run because of:

- Windows permissions
- filesystem permissions
- temporary-directory restrictions
- Docker Desktop
- Docker daemon access
- PostgreSQL availability
- local ports
- environment variables
- API keys
- VPN or proxy configuration
- unavailable external services
- sandbox limitations
- differences between the Codex environment and the user's Windows environment

then:

1. Do not create workaround scripts.
2. Do not modify unrelated production code to force the test to run.
3. Stop after identifying the environment limitation.
4. Tell the user exactly which command to run locally.
5. Tell the user which traceback, logs, or output to return.
6. Wait for the real local result before changing production code based on assumptions.

The user's real local environment is the source of truth for environment-specific integration failures.

---

## 4. Testing Priorities

### Unit tests

Prefer isolated tests for deterministic logic such as:

- input validation
- order ID validation
- refund eligibility rules
- date/time rules
- state transitions
- deterministic business rules
- routing helper functions

Avoid unnecessary LLM, network, Docker, or database dependencies in unit tests.

### Integration tests

Test boundaries such as:

- LangGraph → tools
- Agent → PostgreSQL
- persistence/checkpoint behavior
- interrupt/resume behavior
- tool calls
- routing between workflow branches

If required infrastructure is unavailable, provide the user with the exact local test command.

### End-to-end tests

Do not assume the complete system works merely because unit tests pass.

For tests requiring real:

- LLM calls
- PostgreSQL
- Docker Compose
- API keys
- external APIs
- human-in-the-loop interaction

allow the user to perform the final local validation when necessary.

---

## 5. Bug Fixing

When a test fails:

1. Read the actual traceback or logs.
2. Determine whether the problem comes from:
   - application logic
   - test logic
   - dependency/version mismatch
   - environment/configuration
   - external infrastructure
3. Fix the underlying cause.
4. Do not weaken assertions merely to make tests pass.
5. Do not hide failures with broad exception handling.
6. Do not change correct production behavior to satisfy an incorrect test.
7. Add a regression test when fixing a meaningful bug.

If there is insufficient evidence, request the relevant traceback or logs instead of guessing.

---

## 6. Docker and Docker Compose

Docker-related implementation may generally be automated.

You may:

- create or update `Dockerfile`
- create or update `compose.yaml`
- create or update `.dockerignore`
- improve build caching
- update service configuration
- add appropriate health checks
- update Docker-related documentation
- prepare Docker validation commands

However:

- Never copy `.env` into a Docker image.
- Never hard-code API keys or database passwords.
- Keep secrets outside images.
- Prefer runtime environment variables.
- Avoid unnecessary Docker complexity.
- Do not introduce infrastructure without a concrete requirement.
- Preserve PostgreSQL data with appropriate volumes when persistence is required.
- Explain meaningful changes to ports, networks, volumes, images, or services.

After Docker changes, provide the exact commands the user should run locally to verify them.

---

## 7. PostgreSQL and Persistence

Treat database changes carefully.

Before changing:

- schemas
- tables
- migrations
- persistence logic
- initialization
- stored data structures

explain:

1. What will change.
2. Why it is needed.
3. Whether existing data may be affected.

Do not silently:

- drop tables
- delete persisted data
- reset databases
- remove Docker volumes containing project data

If verification requires the user's PostgreSQL or Docker environment, provide local commands instead of claiming success.

---

## 8. Environment Variables and Secrets

Never commit secrets.

Keep files such as these excluded when appropriate:

- `.env`
- credential files
- API key files
- private certificates
- local database credentials

Use `.env.example` for documentation.

`.env.example` should contain:

- variable names
- safe placeholder values

It must never contain real secrets.

---

## 9. Git and GitHub Rules

The user owns final Git version-control operations.

Codex may use read-only Git commands to understand the repository, including:

- `git status`
- `git diff`
- `git diff --staged`
- `git log`
- `git show`
- `git branch --show-current`
- other non-destructive inspection commands

Codex may also:

- review diffs
- suggest commit messages
- summarize changes
- recommend version numbers
- prepare release notes
- update `.gitignore`
- update README files

### Do not perform final Git writes

Unless the user explicitly overrides this rule for a specific operation, Codex must NOT execute:

- `git add`
- `git commit`
- `git tag`
- `git push`
- `git merge`
- `git rebase`
- `git reset`
- `git cherry-pick`
- branch deletion
- force push
- history rewriting
- other destructive or history-changing Git operations

Instead:

1. Finish the code changes.
2. Show the user what changed.
3. Suggest the exact Git commands the user should run.
4. Let the user execute the final Git operations in their own Windows environment.

### Windows Git ownership

Do not modify repository ownership merely to make Git commands work.

Do not automatically execute or modify:

- `git config --global --add safe.directory ...`
- Windows ownership settings
- filesystem ACLs
- `takeown`
- permission configuration

If Git reports:

- `dubious ownership`
- permission errors
- ownership mismatches
- sandbox-related Git errors

stop and report the issue to the user.

The user will resolve Git ownership or `safe.directory` configuration in their own Windows account.

Do not create a new repository or copy the repository elsewhere merely to bypass an ownership problem.

---

## 10. Git Cleanliness

Before recommending a commit:

- inspect changed files
- ensure `.env` or secrets are not included
- ensure temporary/debug files are not included
- identify unrelated modifications
- summarize what should be committed

Do not claim a commit, tag, push, or release exists unless it was actually performed and verified.

---

## 11. Documentation

Keep documentation synchronized with meaningful changes.

Update documentation when changes affect:

- setup
- environment variables
- Docker commands
- PostgreSQL requirements
- architecture
- API usage
- testing
- deployment

Documentation should describe the actual current project rather than planned future functionality.

---

## 12. Dependencies

Do not add dependencies unnecessarily.

When adding or upgrading an important dependency:

- explain why it is needed
- prefer stable and maintained packages
- verify APIs against official documentation
- avoid broad dependency upgrades unrelated to the task

Do not upgrade major framework versions merely because newer versions exist.

---

## 13. Temporary Files and Project Cleanliness

Do not leave behind:

- scratch scripts
- temporary tests
- debug files
- copied secrets
- unnecessary logs
- experimental configuration files

Prefer the existing project structure instead of creating new directories without a clear reason.

---

## 14. Task Completion Report

At the end of a meaningful task, report:

1. What changed.
2. Which files changed.
3. Why the changes were made.
4. Which tests actually ran successfully.
5. Which tests could not run and why.
6. The exact local commands the user should run for remaining validation.
7. Recommended Git commands, if appropriate.
8. Known risks or follow-up work.

Never report a test, build, Docker startup, database operation, commit, push, tag, or deployment as successful unless it was actually verified.