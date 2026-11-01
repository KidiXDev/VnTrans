# VnTrans Repository Guide

VnTrans extracts, translates, and rebuilds Kirikiri visual novel scripts while
preserving commands and unchanged content. It is one Python 3.13+ package with a
GUI-independent API/CLI and an optional Slint desktop GUI, managed with `uv`.
Supported inputs are plain KAG `.ks` and unencrypted PSB v3 `.scn` scripts.

## Working Rules

- Inspect relevant files and state a short plan before implementing; make changes
  incrementally, run relevant checks, and summarize the result briefly.
- Keep edits scoped, reuse existing utilities, and avoid unrelated rewrites or
  unnecessary dependencies. Prefer readable, small functions and useful comments.
- Ask before major architectural changes; run destructive commands only when
  explicitly instructed. Preserve unrelated work and never expose secrets or
  environment values.

## Repository Map

- `src/vntrans/__init__.py`: public API exports; `cli.py`: CLI entry point.
- `src/vntrans/pipeline.py`: shared extraction, validation, rebuilding, and safe
  output publication. `model.py` defines `Unit`, `Script`, and `Engine` contracts;
  `text.py` protects controls and renders/wraps visible text.
- `src/vntrans/engines/`: explicit engine registry; `kirikiri/ks.py` scans KAG,
  `kirikiri/scn.py` maps scenario fields, and `kirikiri/psb.py` handles PSB binaries.
- `src/vntrans/workspace.py`: workspace creation, copied sources, immutable
  extraction, SQLite translations, and import/export.
- `src/vntrans/translation/`: prompts, glossary, provider adapters, quality checks,
  and translation/review orchestration.
- `src/vntrans/gui/`: Python event handling and background jobs. Its `ui/`
  subdirectory holds authored Slint views, shared widgets, and theme;
  `src/vntrans/gui/app.py` loads `ui/main.slint` from that GUI directory.
- `tests/`: standard-library `unittest` suites; `tests/fixtures.py` supplies
  synthetic PSB fixtures.

## Commands

Run these from the repository root. `uv run` manages the project environment;
include `--extra gui` for desktop work and tests that import the GUI.

| Purpose | Command |
| --- | --- |
| Launch desktop app | `uv run --extra gui vntrans-gui` |
| CLI usage | `uv run vntrans --help` |
| Full test suite | `uv run --extra gui python -m unittest discover -s tests` |
| Focused suite (example) | `uv run --extra gui python -m unittest discover -s tests -p test_workspace.py` |
| Build distributions | `uv build` |

See `README.md` for extraction, validation, and rebuild command examples;
`pyproject.toml` defines dependencies, extras, entry points, and the build backend.

## Architecture and Data Boundaries

- Keep the public API and CLI independent of Slint. Put shared batch behavior in
  `pipeline.py`, format-specific behavior in engine handlers, and shared text
  protection/rendering in `text.py`.
- Preserve unit IDs, protected placeholder order, engine commands, encodings,
  and untouched content. Reuse path/overlap checks and staged publication in
  `pipeline.py`; failed processing must not publish partial output or overwrite
  source data or an existing extraction/rebuild destination.
- Workspace `source/` and `extracted/` are snapshots; translation edits belong in
  the SQLite store. Preserve validation and transaction boundaries when saving.
- Keep GUI model and workspace mutations on the event-loop thread. Reuse
  `gui/jobs.py` for background work; `workspace.load_rows()` uses and closes a
  private SQLite connection for worker-thread reads.
- Keep provider wire formats in `translation/providers.py`; reuse the existing
  prompt, decoding, and quality-validation flow for translation/review changes.

## Validation

- Run affected `test_*.py` suites after behavioral changes. Run the full suite for
  shared text/model, pipeline, or cross-layer changes when practical.
- Engine changes need preservation/round-trip checks in `test_ks.py` or
  `test_scn.py`; pipeline safety belongs in `test_pipeline.py`, workspace behavior
  in `test_workspace.py`, AI logic in `test_translation.py`, and GUI logic in
  `test_gui.py` (all under `tests/`).
- GUI tests can skip when its optional dependency is missing. The SCN sample
  tests skip without a local game sample; `sample/` is ignored. Prefer synthetic
  fixtures over adding game assets.
- Report automated checks separately from actual desktop, live-provider, and
  target-game verification. For instruction-only edits, check paths, command
  evidence, and whitespace without running the application test suite.
