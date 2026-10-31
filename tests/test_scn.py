from pathlib import Path
import struct
import unittest
import zlib

from fixtures import make_psb, scenario
from vntrans import VnTransError
from vntrans.engines.kirikiri.psb import PSB
from vntrans.engines.kirikiri.scn import ScnScript
from vntrans import text


def plain(node):
    if node.tag == 33:
        return {key: plain(value) for key, value in node.value.items()}
    if node.tag == 32:
        return [plain(value) for value in node.value]
    return node.value


class ScnTests(unittest.TestCase):
    def test_noop_is_identical(self):
        data = scenario()
        script = ScnScript(data, 0)
        self.assertEqual(script.rebuild({}), data)
        self.assertEqual(script.rebuild({u.id: u.source for u in script.units}), data)

    def test_occurrence_isolation_and_resource_preservation(self):
        original = scenario(many_strings=True)
        script = ScnScript(original, 0)
        unit = next(u for u in script.units if u.kind == "dialogue")
        translated = "Longer Unicode translation café 世界 " * 20
        result = script.rebuild({unit.id: translated})
        before, after = PSB(original), PSB(result)
        old_message = before.root.value["scenes"].value[0].value["texts"].value[0].value[2]
        new_message = after.root.value["scenes"].value[0].value["texts"].value[0].value[2]
        self.assertEqual((old_message.tag, new_message.tag), (21, 22))
        expected = plain(before.root)
        expected["scenes"][0]["texts"][0][2] = translated
        self.assertEqual(plain(after.root), expected)
        self.assertEqual(before.resource_data, after.resource_data)
        self.assertEqual(before.resource_offsets, after.resource_offsets)
        self.assertEqual(before.strings, after.strings[:len(before.strings)])
        self.assertEqual(script.rebuild({unit.id: translated}), result)

    def test_display_override_keeps_identifier(self):
        script = ScnScript(scenario(), 0)
        speaker = next(u for u in script.units if u.kind == "speaker")
        result = plain(PSB(script.rebuild({speaker.id: "Alice"})).root)
        self.assertEqual(result["scenes"][0]["texts"][0][:2], ["internal-id", "Alice"])

    def test_multilingual_dialogue_and_choices(self):
        script = ScnScript(scenario(multilingual=True), 1)
        units = {u.kind: u for u in script.units}
        self.assertEqual(units["dialogue"].source, "EN text")
        self.assertEqual(units["choice"].source, "EN choice")
        changed = script.rebuild({units["dialogue"].id: "New text", units["choice"].id: "New choice"})
        obj = plain(PSB(changed).root)["scenes"][0]
        self.assertEqual(obj["texts"][0][1][0], ["JP name", "JP text", "speech", "search"])
        self.assertEqual(obj["texts"][0][1][1], ["EN name", "New text", "speech", "search"])
        self.assertEqual(obj["selects"][0]["text"], "Same text")
        self.assertEqual(obj["selects"][0]["language"][1]["text"], "New choice")

    def test_empty_translation(self):
        script = ScnScript(scenario(), 0)
        unit = next(u for u in script.units if u.kind == "dialogue")
        changed = plain(PSB(script.rebuild({unit.id: ""})).root)
        self.assertEqual(changed["scenes"][0]["texts"][0][2], "")

    def test_wrap_and_controls(self):
        data = make_psb({"name": "test", "scenes": [{"texts": [[None, None, r"Hello %Bworld%r\nBye"]]}]})
        script = ScnScript(data, 0)
        unit = script.units[0]
        self.assertEqual(unit.source, "Hello ⟦0⟧world⟦1⟧\nBye")
        output = script.rebuild({unit.id: "One ⟦0⟧two⟦1⟧ three"}, wrap_columns=7)
        text = plain(PSB(output).root)["scenes"][0]["texts"][0][2]
        self.assertEqual(text, r"One %Btwo%r\nthree")
        for value in ("Missing", "⟦1⟧⟦0⟧", "⟦0⟧%Bnew⟦1⟧"):
            with self.subTest(value=value), self.assertRaises(VnTransError):
                script.rebuild({unit.id: value})

    def test_ruby_readings_may_be_left_out(self):
        unit = text.make_unit("u", "dialogue", "%D$vl1;「本当の[ざ]座[す]主様」", "scn")
        self.assertEqual(unit.source, "⟦0⟧「本当の⟦1⟧座⟦2⟧主様」")
        self.assertEqual(text.plain(unit.source, unit.context["controls"], "scn"), "⟦0⟧「本当の座主様」")
        self.assertEqual(text.render(unit, "⟦0⟧\"Zasu-sama\"", "scn"), "%D$vl1;\"Zasu-sama\"")
        self.assertEqual(text.render(unit, "⟦0⟧⟦1⟧Za⟦2⟧su", "scn"), "%D$vl1;[ざ]Za[す]su")
        self.assertEqual(text.render(unit, "⟦0⟧Za⟦2⟧su", "scn"), "%D$vl1;Za[す]su")
        for broken in ("\"Zasu-sama\"", "⟦0⟧⟦2⟧Za⟦1⟧su", "⟦0⟧⟦1⟧⟦1⟧Zasu"):
            with self.assertRaisesRegex(VnTransError, "placeholders"):
                text.render(unit, broken, "scn")

    def test_invalid_binaries_and_layouts(self):
        data = scenario()
        for invalid in (b"PSB", data[:100], b"mdf\0", data[:4] + b"\x04" + data[5:],
                        data[:6] + b"\x01" + data[7:]):
            with self.subTest(length=len(invalid)), self.assertRaises(VnTransError):
                PSB(invalid)
        with self.assertRaises(VnTransError):
            ScnScript(make_psb({"name": "asset", "scenes": []}), 0)
        with self.assertRaises(VnTransError):
            ScnScript(scenario(multilingual=True), 9)
        damaged = bytearray(data)
        entries = struct.unpack_from("<I", damaged, 36)[0]
        damaged[entries] = 0xff
        with self.assertRaisesRegex(VnTransError, "tag"):
            PSB(bytes(damaged))

    def test_header_checksum(self):
        script = ScnScript(scenario(), 0)
        unit = next(u for u in script.units if u.kind == "dialogue")
        data = script.rebuild({unit.id: "Translated"})
        self.assertEqual(struct.unpack_from("<I", data, 40)[0], zlib.adler32(data[8:40]))

    def test_scalar_types_and_three_byte_string_offsets(self):
        script = ScnScript(scenario(), 0)
        original = plain(script.psb.root)["untouched"]
        self.assertEqual(original[:3], [None, False, True])
        unit = next(u for u in script.units if u.kind == "dialogue")
        output = script.rebuild({unit.id: "x" * 70000})
        self.assertEqual(plain(PSB(output).root)["untouched"], original)


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "sample/01_com_001_01.ks.scn"
        if not path.exists():
            raise unittest.SkipTest("local game sample is not distributed")
        cls.data = path.read_bytes()

    def test_sample_records_and_byte_identical_noop(self):
        script = ScnScript(self.data, 0)
        self.assertEqual(sum(u.kind == "dialogue" for u in script.units), 152)
        self.assertEqual(script.rebuild({}), self.data)

    def test_sample_only_selected_text_changes(self):
        script = ScnScript(self.data, 0)
        dialogue = next(u for u in script.units if u.kind == "dialogue")
        before = plain(PSB(self.data).root)
        before["scenes"][0]["texts"][0][2] = "A translated night scene."
        rebuilt = script.rebuild({dialogue.id: "A translated night scene."})
        self.assertEqual(plain(PSB(rebuilt).root), before)
