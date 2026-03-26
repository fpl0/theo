# Dev scripts

## Context

Every dev workflow (start infra, run agent, lint, format, test) required typing full commands from memory or copying from CLAUDE.md. A task runner provides short aliases.

## Decisions

**`just` over Makefile or poethepoet.** Makefile is zero-install but has dated syntax (tab-sensitivity, clunky variable passing, no `.ONESHELL` on macOS Make 3.81). poethepoet integrates into pyproject.toml but adds a runtime dev dependency and longer invocation (`uv run poe`). `just` is a single binary (`brew install just`), has clean syntax with native recipe arguments, and fits the modern tooling aesthetic alongside uv/ruff/ty.

**Fail-fast quality gate.** `just check` runs tools fastest-first (ruff → sqlfluff → ty → pytest) so the cheapest checks fail first. Each line in a just recipe stops on first non-zero exit.

**Format order: fix then format.** `just fmt` runs `ruff check --fix` before `ruff format` because auto-fixes may change code that the formatter then normalizes.

**Default recipe is help.** Running `just` with no arguments lists all targets with descriptions (just's built-in `--list` behavior via comment annotations).

## Files changed

- `justfile` — new, all dev targets
- `CLAUDE.md` — Quick Start, Testing, and Adding new modules sections updated
