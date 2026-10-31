# VnTrans

A Python translation pipeline for extracted visual novel scripts:

```text
GARbro / external unpacker → original scripts → translation JSON → rebuilt scripts
```

V1 supports unencrypted PSB v3 Kirikiri scenarios (`.scn`, including `.ks.scn`)
and plain Kirikiri/KAG `.ks` scripts. The core uses only the Python standard library
and works through a CLI or a Python API. An optional desktop app adds project
workspaces and LLM translation (Gemini and OpenRouter). Python 3.13 or newer is required.

## Quick start

From this repository, with [uv](https://docs.astral.sh/uv/):

Extract your game's scenario scripts with GARbro or another external unpacker.
The examples below use `unpacked/scenario` as the directory containing those scripts.

```sh
uv sync
uv run vntrans extract unpacked/scenario --output work/json --engine kirikiri
```

Edit only `translation` fields in the generated JSON files, then run:

```sh
uv run vntrans validate unpacked/scenario --translations work/json
uv run vntrans rebuild unpacked/scenario --translations work/json --output work/patch
```

Both files and directories are accepted. Directory inputs are scanned recursively
for `.ks` and `.scn`; other assets are ignored. File contents determine whether a
candidate is PSB or plain KS. JSON files retain the complete source filename:
`scenario/a.ks.scn` becomes `scenario/a.ks.scn.json`. Rebuilt files retain their
original paths and extensions.

Output directories must be new, separate from both originals and translations.
Existing destinations are rejected before reading or parsing the batch, and
checked again before publishing. VnTrans validates the entire batch before
publishing it. An error leaves the originals untouched and publishes no output
batch. Existing output directories are
never intentionally replaced; choose a new directory for each rebuild.

The CLI shows an animated spinner, stage progress, and the current filename on
stderr while scanning, loading JSON, processing scripts, and writing output.
Redirected stderr uses ordinary progress lines without animation; stdout contains
the final summary. Ctrl+C stops processing and returns exit status 130.

You can also install with `pip install .`, or run directly from the source tree:

```sh
PYTHONPATH=src python -m vntrans --help
```

## Desktop app

```sh
uv sync --extra gui
uv run vntrans-gui                 # or: uv run vntrans-gui path/to/workspace
```

The app is built with [Slint](https://slint.dev). Everything happens in a
**workspace folder** that you choose when creating a project:

```text
<workspace>/
  manifest.json        project settings (languages, prompt, batch and build options)
  source/              copies of the supported game scripts; originals are only read
  extracted/           translation JSON from extraction, never edited
  project.db           SQLite: translations, glossary, review suggestions, request log
  builds/<time>/json/  complete translation JSON, usable with the CLI
  builds/<time>/patch/ rebuilt scripts
  exports/             glossary exports
```

- **Texts**: every unit in a virtualized list. Search by source, translation,
  speaker or ID, with plain text or regular expressions and case matching. Filter
  by file, kind and status (new, machine, edited, failed, excluded, issues,
  review). Speaker-name units are listed only under the "Speaker names" kind;
  the default "All lines" shows them in the speaker column. Edits are checked
  with the same placeholder rules as rebuild before they are saved, and a saved
  translation also fills identical units (same kind and source) that are
  untranslated, machine-translated or still match the old text. Ctrl+Enter
  saves and moves to the next unit. Find & replace works on the translations
  currently shown. On first open, units without any letters of the source
  language (`……`, `？？？`, digits, symbols) are excluded so they keep their
  original text; Undo brings them back.
- **Glossary**: terms and characters with notes. Only approved entries whose
  source text appears in a batch are sent to the model. Import and export as JSON
  (`source`/`target`/`notes`, or `src`/`dst`/`info`). Replacement rules, one per
  line (`pre|post FIND => REPLACE`, `/FIND/` for a regex), rewrite source text
  before sending (`pre`) or model output (`post`), outside placeholders only.
- **Prompt**: an ordered chain of sections (role, style, project notes,
  characters, glossary, context, prompt enhancement, fixed output rules).
  Toggle, reorder, edit or reset each one, and preview the exact messages for
  the batch around the selected unit, with an estimated token count. Prompt
  enhancement (on by default, as in LinguaGacha) has the model write brief
  structured reasoning in a `why` field before its translations; it costs a
  few output tokens per batch and shows in the batch inspector's thinking.
- **Translate**: runs a profile over new units or the current Texts filter, in
  batches that stay within one scene, with nearby lines as context. Replies are
  checked unit by unit. Valid units are saved immediately, and failed ones are
  retried in smaller batches, then individually. Rate limits and network errors
  are retried with backoff, and bad keys stop the run. Stopping or closing loses
  nothing that was already accepted, and the next run continues with the rest.
  Manual edits are never overwritten. Speaker names are filled from the glossary,
  and identical units (same kind and source, project-wide) are sent once; new
  units reuse an existing translation of the same text without a request. The same page runs **Review translations**
  (suggestions you accept or dismiss in Texts) and **Discover terms** (draft
  glossary entries with example lines, for you to approve).
- **Build**: exports the JSON and rebuilds a patch with optional wrapping and
  output encoding. **Export LinguaGacha** writes
  [LinguaGacha](https://github.com/neavo/LinguaGacha) MESSAGE JSON
  (`[{"name", "message"}]`, one file per script) to `exports/linguagacha-<time>/`.
  **Import LinguaGacha** reads LinguaGacha's translated output folder back, by
  file path and line position; a translated `name` fills that speaker everywhere.
  Imports fill only new and machine units and pass the same checks as edits. Add
  a LinguaGacha text-preserve rule for `⟦\d+⟧` so placeholders survive.
- **Settings**: provider profiles for Google Gemini (native API) and OpenRouter:
  model, keys (several rotate), temperature, output limit, reasoning level and
  timeout, with model listing and a test request.

The window reopens with its last size and maximized state, and on Windows at
its last position and monitor. It never starts minimized, and a position that
is off-screen (for example after unplugging a monitor) is moved back onto a
screen.

Profiles are stored outside workspaces in your user config folder
(`~/.config/vntrans/config.json` on Linux, mode 0600). Set
`VNTRANS_<PROFILE_NAME>_KEY` (comma-separated) to keep keys out of the file.
Token usage is shown as reported by the provider; the app doesn't estimate cost.
The folder picker uses the desktop portal on Linux, which needs the `jeepney`
package (installed with the `gui` extra); you can always type a path instead.

## Translation JSON

Each document includes `schema_version`, `engine`, `format`, `source`, `sha256`,
`encoding`, `language_index`, and `units`. A unit looks like:

```json
{
  "id": "scenes/0/texts/0/2",
  "kind": "dialogue",
  "source": "Original text ⟦0⟧",
  "translation": null,
  "context": {
    "scene": 0,
    "label": "*start",
    "speaker": null,
    "original": "Original text %r",
    "controls": ["%r"]
  }
}
```

- Set `translation` to translated text. `null` retains the original.
- `""` clears a unit without protected controls. To clear visible text around
  controls, retain its placeholders.
- Keep every `⟦0⟧`, `⟦1⟧`, etc. exactly once and in its original order. They
  represent protected tags, controls, or ruby spans. Delimiters `⟦` and `⟧` are
  reserved. See `context.original` and `context.controls` for their contents.
- JSON `\n` inside a translation means an explicit line break. VnTrans writes
  the format's native break (`[r]` for dialogue in KS, `\n` in SCN).
- Units may be reordered. IDs, source text, context, and document metadata must
  remain unchanged. All original units and JSON files are required.
- Identical text has separate occurrence IDs and may have separate translations.

This is VnTrans's own versioned schema, not VNTranslationTools JSON. Source hashes
prevent accidentally applying translations to different source files. Translation
files are decoded as UTF-8; duplicate JSON keys and invalid values are rejected.

## Encoding, languages, and wrapping

KS scripts retain their BOM, encoding, comments, line endings, and untouched spans.
UTF-8 and UTF-16 BOMs are detected; BOM-less text defaults to CP932. Override the
input interpretation when necessary:

```sh
uv run vntrans extract unpacked/scenario --output work/json --encoding utf-8
```

The chosen encoding is stored in JSON. Unsupported output characters are errors,
never silently replaced. An explicit override converts text scripts only; PSB
always uses UTF-8 internally:

```sh
uv run vntrans rebuild unpacked/scenario --translations work/json \
  --output work/patch --output-encoding utf-16-le
```

UTF-16 output overrides include a BOM. Use `utf-8-sig` for UTF-8 with a BOM or
`utf-8` without one. Check that the target game accepts the chosen encoding and
has fonts for the translated characters.

Multilingual SCN files default to language index 0. Choose another slot during
extraction with `--language-index N`. Other language slots remain untouched.
The selected slot must exist in every dialogue and choice record in the batch;
plain KS files do not support language-index selection.

Automatic wrapping is opt-in and applies only to changed dialogue translations:

```sh
uv run vntrans validate unpacked/scenario --translations work/json --wrap-columns 48
uv run vntrans rebuild unpacked/scenario --translations work/json \
  --output work/patch --wrap-columns 48
```

The number is a **column limit**, not a pixel width. Latin text wraps at
whitespace. Wide/fullwidth characters count as two columns and combining marks
as zero. Explicit breaks are retained. Control codes take no width; ruby spans
remain indivisible, with approximate visible width. Overlong unbreakable words
produce warnings. Names, titles, and choices are not automatically wrapped.
Start with a conservative width and calibrate in the game.

## Python API and engine extensions

```python
from vntrans import extract, validate, rebuild

report = extract("unpacked/scenario", output="work/json", engine="kirikiri")
# Edit JSON translations outside VnTrans.
validate("unpacked/scenario", translations="work/json", wrap_columns=48)
rebuild("unpacked/scenario", translations="work/json", output="work/patch",
        wrap_columns=48)
```

Operations return a `Report` with file/unit counts and warnings. Invalid or
unsupported data raises `VnTransError`; filesystem failures raise `OSError`.
The CLI reports either error on stderr with a nonzero exit status.

The Python API stays quiet by default. Pass a `progress` callback accepting
`(phase, completed, total, filename)` to receive updates; `total == 0` means the
total is not yet known. Completed counts are local to each phase.

The core is independent of CLI and GUI code. `pipeline.py` owns JSON and batch
operations; `model.py` defines the handler contracts. Engine implementations live
under `engines/`, with a small explicit registry in `engines/__init__.py`.

To add an engine, implement `probe(name, header)` and
`extract(name, bytes, encoding=..., language_index=...)`. Return an object with
`format`, `encoding`, `units`, `warnings`, and `rebuild(...)`. Register it once.
Each handler owns its locators and serialization; the shared pipeline validates
the public translation document. No plugin loader or external service is needed.

## Supported boundaries

- SCN extraction follows known `scenes/texts` and `scenes/selects` layouts,
  including supported multilingual records. Internal speaker IDs remain unchanged;
  display-name overrides are created only when translated.
- PSB rebuilding preserves original scalar representations, existing string IDs,
  resource bytes, and non-text data. Shared strings are replaced per occurrence.
  Offsets and the header checksum are rebuilt. Unchanged imports return the
  original file bytes; changed files may have different binary layouts.
- KS extraction handles ordinary dialogue, label titles, `#` speaker lines,
  `link` choice captions, and recognized speaker/choice attributes. It skips
  comments, script blocks, macro definitions, expressions, and unknown attributes.
  Game-specific attribute mappings are centralized in the Kirikiri KS module.
- Ruby is preserved as protected spans and reported as untranslated. Unknown
  commands remain protected; they are never executed.
- Encrypted PSB, PSB versions other than 3, MDF wrappers, scrambled KS,
  multiline KS tags, TJS bytecode, and unsupported scenario layouts are rejected.
  Decode wrappers externally before using VnTrans.

There is no archive extraction/packing, font patching, or game-runtime
integration in v1. Use an external packer to put rebuilt files
into a patch archive recognized by your game. Loose files may not override its
archives; this depends on the game. Never overwrite the only copy of a game.

## Verification

```sh
uv run python -m unittest discover -s tests -v
uv build
```

Tests include independent synthetic binary fixtures, multilingual dialogue and
choices, shared strings, resources, long translations, encoding conversions,
control protection, wrapping, JSON validation, and safe batch output. These
synthetic fixtures allow the core test suite to run without proprietary game files.

For a manual game check:

1. Extract the game's scripts externally and keep the originals.
2. Translate a dialogue line, a displayed name, and a choice from real scripts.
3. Validate, rebuild, and package them using the game's existing patch workflow.
4. Launch the game and visit those exact scenes. Check names, choices, line
   breaks, fonts, page boundaries, and save/load behavior.
5. Adjust the wrap width and repeat with a new output directory.

Binary round trips are tested; in-game compatibility and layout must be checked
against the target game. References and license notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

VnTrans is distributed under the [MIT License](LICENSE). Adapted third-party
code retains the attribution and notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
