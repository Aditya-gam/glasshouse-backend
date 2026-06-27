# Rules — Code Style

Cross-cutting conventions. Govern implementation. (≤200 lines.)

## Python
- **PEP 8 + `ruff`** — the style standard; `ruff` lints **and** formats (replaces black/isort/flake8/autoflake, 600+ rules). *Why:* one consistent auto-fixable style.
- **Type annotations everywhere (PEP 484) + `mypy` strict** — explicit params/returns, **no implicit `Any`**. *Why:* the checker converts runtime errors to build-time errors; annotations document intent.
- **Google-style docstrings for the non-obvious** — describe semantics; **omit types already in annotations**. *Why:* don't duplicate the type info.
- **Naming** — `snake_case` funcs/vars, `PascalCase` classes, `UPPER_SNAKE` constants. **Small, single-purpose functions; early returns over deep nesting.** *Why:* readable, testable.
- **Comments explain *why*, not *what*; pass dependencies explicitly (no hidden globals).** *Why:* code says what; testability needs explicit deps.

## TypeScript
- **`strict` mode, no `any`** — type at the boundaries; **`Zod` to parse/validate** external data (don't assert). *Why:* `any` defeats the type system; runtime data must be validated.
- **Explicit return types on exported functions.** **Function components + hooks, `async/await`** — no class components, no callback-style async. *Why:* stable contracts; modern, less error-prone idioms.

## Git & changes
- **Conventional Commits** — `type(scope): summary` (`feat`/`fix`/`docs`/`refactor`/`test`/`chore`). *Why:* machine-readable history, clear intent, easy changelogs.
- **One logical change per commit; one migration per schema change.** *Why:* bisectable, reviewable, revertible.

## General
- **Match the surrounding code** — its naming, comment density, idioms. *Why:* consistency beats personal preference.
- **Don't over-engineer** — build what's asked; no speculative abstractions or defensive code for impossible states; the right complexity is the minimum the task needs. *Why:* simpler code, fewer bugs.

## Sources
- [PEP 8](https://peps.python.org/pep-0008/) · [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) · [PEP 484 — Type Hints](https://peps.python.org/pep-0484/) · [Ruff](https://docs.astral.sh/ruff/) · [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/) · [Conventional Commits](https://www.conventionalcommits.org/).
