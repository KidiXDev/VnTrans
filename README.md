# VnTrans

VnTrans helps you translate visual novels without editing game scripts
by hand. Extract text, translate and review it, then rebuild scripts while
preserving game commands and unchanged content.

- Kirikiri/KAG plain `.ks` scripts and unencrypted PSB v3 `.scn` files, including
  `.ks.scn`.
- HexenHaus NORI 1.0 scripts (`.bin`) and ARCC v2 scenario archives (`rio.arc`).

## What it can do

- Edit translations in a desktop app with search, filters, bulk actions and Undo.
- Translate with Gemini or OpenRouter using custom prompts and a shared glossary.
- Review translations and accept or reject suggestions individually or all at once.
- Import and export translations for LinguaGacha.
- Extract, validate and rebuild translation JSON through the CLI.

Source quotations such as `「…」` become `“…”`; unquoted narration stays unquoted.

## Clone and run

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/KidiXDev/VnTrans.git
cd VnTrans
uv run --extra gui vntrans-gui
```

1. For Kirikiri, unpack the game's scripts with GARbro or another external unpacker.
2. Create a workspace and select the engine. Choose the unpacked script folder
   for Kirikiri, or select `data/rio.arc` or a standalone `.bin` for HexenHaus.
3. Edit manually, or add your Gemini/OpenRouter API key in Settings to translate
   and review with AI.
4. Build a patch. For HexenHaus, the rebuilt archive can replace a backed-up
   `data/rio.arc` in the game folder.

Manual editing and the CLI work offline. AI features require your own provider
API key.

## Command line

Extract scripts into editable JSON:

```sh
uv run vntrans extract unpacked/scenario --output work/json
```

Edit only the `translation` fields. Keep protected placeholders such as `⟦0⟧`
in their original order, then validate and rebuild:

```sh
uv run vntrans validate unpacked/scenario --translations work/json
uv run vntrans rebuild unpacked/scenario --translations work/json --output work/patch
```

Choose a new output folder for each extraction or rebuild. Run
`uv run vntrans --help` for more options.

For HexenHaus, select the engine during extraction and use the same archive
for validation and rebuilding.

```sh
uv run vntrans extract data/rio.arc --engine hexenhaus --output work/json
uv run vntrans validate data/rio.arc --translations work/json
uv run vntrans rebuild data/rio.arc --translations work/json --output work/patch
```

HexenHaus translations can exceed the original byte length. When a translation
contains dashes or Latin accents outside CP932, the build uses compatible
characters where possible. With wrapping enabled, the builder can expand a dialogue
to three lines and reports dialogues that still exceed the target width. Without
wrapping, excess lines are joined into the last slot. Test text width and wrapping
in the target game.

## Development

Ruff handles Python linting, import sorting, and formatting. It is included in
the default `uv` development dependencies and configured in `pyproject.toml`.
The configuration targets Python 3.13 and enables style, import ordering,
modernization, common bug, simplification, and unused suppression checks.
Formatting uses Ruff's defaults with an 88-character line length. The linter's
line-length rule is disabled because the formatter leaves some strings intact.

```sh
uv run ruff check .
uv run ruff format --check .
```

To apply automatic lint fixes and formatting:

```sh
uv run ruff check --fix .
uv run ruff format .
```

Run the test suite with the optional GUI dependency installed:

```sh
uv run --extra gui python -m unittest discover -s tests
```

## License

[MIT](LICENSE). Third-party attributions are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
