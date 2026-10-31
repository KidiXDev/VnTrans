"""Scenario field mapping; internal game identifiers are never translated."""

from ...model import VnTransError
from ...text import make_unit, render
from .psb import Node, PSB


def string(node):
    return node.value if node is not None and 21 <= node.tag <= 24 else None


class ScnScript:
    format = "scn"
    encoding = None

    def __init__(self, data: bytes, language_index: int):
        self.data = data
        self.psb = PSB(data)
        self.units = []
        self.warnings = []
        root = self.psb.root
        if root.tag != 33 or "scenes" not in root.value or "name" not in root.value:
            raise VnTransError("PSB is not a supported scenario (expected name and scenes)")
        scenes = root.value["scenes"]
        if scenes.tag != 32:
            raise VnTransError("scenario scenes must be a list")
        for si, scene in enumerate(scenes.value):
            if scene.tag != 33:
                raise VnTransError(f"scenes/{si}: expected a dictionary")
            scene = scene.value
            context = dict(scene=si, label=string(scene.get("label")))
            if string(scene.get("title")):
                self.add(f"scenes/{si}/title", "title", scene, "title", **context)
            texts = scene.get("texts")
            if texts is not None:
                if texts.tag != 32:
                    raise VnTransError(f"scenes/{si}/texts: expected a list")
                for ti, record in enumerate(texts.value):
                    base = f"scenes/{si}/texts/{ti}"
                    if record.tag != 32 or len(record.value) < 2:
                        raise VnTransError(f"{base}: unsupported text record")
                    row = record.value
                    real_name = string(row[0])
                    context_row = dict(context, speaker=real_name)
                    if row[1].tag == 32:
                        message = row[1]
                        message_index = 1
                    elif len(row) >= 3:
                        message = row[2]
                        message_index = 2
                    else:
                        raise VnTransError(f"{base}: missing message")
                    if message.tag == 32:
                        langs = message.value
                        if language_index >= len(langs):
                            raise VnTransError(f"{base}: language index {language_index} is unavailable")
                        language = langs[language_index]
                        if language.tag != 32 or len(language.value) < 2:
                            raise VnTransError(f"{base}: unsupported multilingual record")
                        base += f"/{message_index}/{language_index}"
                        row = language.value
                        if string(row[0]) is not None:
                            self.add(base + "/0", "speaker", row, 0, **context_row)
                        self.add(base + "/1", "dialogue", row, 1, **context_row)
                    elif string(message) is not None:
                        if language_index:
                            raise VnTransError(f"{base}: record has no language {language_index}")
                        if string(row[1]) is not None:
                            self.add(base + "/1", "speaker", row, 1, **context_row)
                        elif row[1].tag == 1 and real_name and real_name != "＠":
                            self.add(base + "/1", "speaker", row, 1, fallback=real_name, **context_row)
                        self.add(base + "/2", "dialogue", row, 2, **context_row)
                    else:
                        raise VnTransError(f"{base}: unsupported message value")
            selects = scene.get("selects")
            if selects is not None:
                if selects.tag != 32:
                    raise VnTransError(f"scenes/{si}/selects: expected a list")
                for ci, choice in enumerate(selects.value):
                    base = f"scenes/{si}/selects/{ci}"
                    if choice.tag != 33:
                        raise VnTransError(f"{base}: expected dictionary")
                    choice = choice.value
                    if "language" in choice and choice["language"].tag == 32:
                        languages = choice["language"].value
                        if language_index >= len(languages) or languages[language_index].tag != 33:
                            raise VnTransError(f"{base}: unavailable choice language")
                        choice = languages[language_index].value
                        base += f"/language/{language_index}"
                    elif language_index:
                        raise VnTransError(f"{base}: choice has no selected language")
                    self.add(base + "/text", "choice", choice, "text", **context)
        if not any(unit.kind in ("dialogue", "choice") for unit in self.units):
            raise VnTransError("scenario contains no supported dialogue or choices")

    def add(self, id, kind, container, key, fallback=None, **context):
        raw = fallback if fallback is not None else string(container.get(key) if isinstance(container, dict) else container[key])
        if raw is None:
            raise VnTransError(f"{id}: expected text string")
        unit = make_unit(id, kind, raw, self.format, **context)
        self.units.append(unit)
        if any(c.startswith("[") for c in unit.context["controls"]):
            self.warnings.append(f"{id}: ruby/annotation spans are protected; translations may leave them out")

    def rebuild(self, translations, *, wrap_columns=None, output_encoding=None):
        if output_encoding:
            raise VnTransError("output encoding overrides apply only to KS files; PSB uses UTF-8")
        # Reparse for repeatable API calls; never mutate the source model.
        psb = PSB(self.data)
        changes = {}
        for unit in self.units:
            if unit.id in translations and translations[unit.id] != unit.source:
                changes[unit.id] = render(unit, translations[unit.id], self.format, wrap_columns)
        if not changes:
            return self.data
        for id, text in changes.items():
            path = id.split("/")
            node = psb.root
            for part in path[:-1]:
                node = node.value[int(part) if node.tag == 32 else part]
            key = int(path[-1]) if node.tag == 32 else path[-1]
            node.value[key] = Node.string(text)
        result = psb.build()
        PSB(result)
        return result
