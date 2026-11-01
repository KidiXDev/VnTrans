# VnTrans

VnTrans helps you translate Kirikiri visual novels without editing game scripts
by hand. Extract text, translate and review it, then rebuild scripts while
preserving game commands and unchanged content.

Supports plain Kirikiri/KAG `.ks` scripts and unencrypted PSB v3 `.scn` files,
including `.ks.scn`.

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

1. Unpack the game's scripts with GARbro or another external unpacker.
2. Create a workspace in VnTrans and select the unpacked scripts folder.
3. Edit manually, or add your Gemini/OpenRouter API key in Settings to translate
   and review with AI.
4. Build a patch and package it using the game's patch workflow.

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

VnTrans does not unpack archives, decode encrypted scripts, repack games or patch
fonts. These steps depend on the game and require external tools. Keep a backup
and test the rebuilt scripts in the target game.

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
