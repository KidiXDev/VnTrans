import codecs
import unittest
import warnings

from vntrans import VnTransError
from vntrans.engines.kirikiri.ks import KsScript
from vntrans.text import make_unit, render, width


class KsTests(unittest.TestCase):
    def parse(self, text):
        return KsScript(codecs.BOM_UTF8 + text.encode())

    def test_preserve_nontext_and_mixed_newlines(self):
        text = ('; comment\r\n*start|Scene title\n@iscript\r\nvar s = "hidden";\n@endscript\n'
                '[macro name=test]\nHidden definition\n[endmacro]\n'
                '[name name="Alice" storage="秘密.ks"]Hello [font color=red]world[resetfont][p]\r\n'
                '[link target="*next"]Choose me[endlink]\n'
                '@button text="Choice two" storage="hidden.ks"\n')
        script = self.parse(text)
        self.assertEqual([u.kind for u in script.units], ["title", "speaker", "dialogue", "choice", "choice"])
        self.assertEqual(script.rebuild({}), script.data)
        unit = next(u for u in script.units if u.kind == "dialogue")
        translated = "Good ⟦0⟧evening⟦1⟧⟦2⟧"
        result = script.rebuild({unit.id: translated})
        expected = text.replace("Hello [font color=red]world[resetfont][p]", "Good [font color=red]evening[resetfont][p]")
        self.assertEqual(result, codecs.BOM_UTF8 + expected.encode())

    def test_quoted_brackets_and_attributes(self):
        script = self.parse('[name name="A]lice"]Hello\n@button text=Choose\n')
        speaker, dialogue, choice = script.units
        result = script.rebuild({speaker.id: 'A "name"', choice.id: "Pick this"})
        self.assertIn(b'name="A \\"name\\""', result)
        self.assertIn(b'text="Pick this"', result)
        self.assertEqual(KsScript(result).units[0].source, 'A "name"')
        script = self.parse('[name name="[Alice]"]Hello\n')
        self.assertEqual(script.units[0].source, "[Alice]")
        result = script.rebuild({script.units[0].id: "[Bob]"})
        self.assertIn(b'name="[Bob]"', result)

    def test_literals_and_line_breaks(self):
        script = self.parse("Hello [[world\n")
        unit = script.units[0]
        self.assertEqual(unit.source, "Hello ⟦0⟧world")
        changed = script.rebuild({unit.id: "Hi ⟦0⟧world [literal]\nBye"})
        self.assertIn(b"Hi [[world [[literal][r]Bye", changed)

    def test_encodings_and_explicit_override(self):
        for codec, bom in (("cp932", b""), ("utf-8", codecs.BOM_UTF8),
                           ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)):
            with self.subTest(codec=codec):
                data = bom + "こんにちは\r\n".encode(codec)
                script = KsScript(data)
                self.assertEqual(script.rebuild({}), data)
                result = script.rebuild({script.units[0].id: "Hello"})
                self.assertEqual(result, bom + "Hello\r\n".encode(codec))
        script = KsScript("こんにちは\n".encode("cp932"))
        with self.assertRaises(VnTransError):
            script.rebuild({script.units[0].id: "😀"})
        result = script.rebuild({script.units[0].id: "😀"}, output_encoding="utf-8-sig")
        self.assertEqual(result, codecs.BOM_UTF8 + "😀\n".encode())
        self.assertEqual(KsScript(b"plain", "utf-8").encoding, "utf-8")

    def test_ruby_is_opaque_and_protected(self):
        script = self.parse('A [ruby text="kanji" char=2]漢字 example[p]\n')
        unit = script.units[0]
        self.assertEqual(unit.source, "A ⟦0⟧ example⟦1⟧")
        changed = script.rebuild({unit.id: "An ⟦0⟧ example⟦1⟧"})
        self.assertIn('[ruby text="kanji" char=2]漢字'.encode(), changed)
        self.assertTrue(script.warnings)
        dropped = script.rebuild({unit.id: "A Chinese character example⟦1⟧"})
        self.assertEqual(dropped.decode("utf-8-sig"), "A Chinese character example[p]\n")
        with self.assertRaises(VnTransError):
            script.rebuild({unit.id: "An example"})

    def test_break_text_inside_tag_attribute_stays_untouched(self):
        script = self.parse('Hello [emb exp="\'[r]\'"] friend\n')
        unit = script.units[0]
        result = script.rebuild({unit.id: "Hi ⟦0⟧ friend"})
        self.assertIn(b'[emb exp="\'[r]\'"]', result)

    def test_no_attribute_expression_extraction(self):
        script = self.parse('[name name="&variable"]Hello\n@button text=%macro\n')
        self.assertEqual([u.kind for u in script.units], ["dialogue"])
        script = self.parse('[name name="Alice"]Hello\n')
        for value in ("&evil()", "%macro", " &evil()"):
            with self.subTest(value=value), self.assertRaises(VnTransError):
                script.rebuild({script.units[0].id: value})

    def test_bad_tags_blocks_unicode_and_command_injection(self):
        for text in ("Hello [font", "@iscript\ncode\n", "[macro name=x]\nhello", "; comment\n"):
            with self.subTest(text=text), self.assertRaises(VnTransError):
                self.parse(text)
        script = self.parse("Hello\n")
        for translated in ("@jump target=*x", ";comment", "\ud800", "bad\x00text", "⟦99⟧"):
            with self.subTest(text=repr(translated)), self.assertRaises(VnTransError):
                script.rebuild({script.units[0].id: translated})
        script = self.parse("  Hello\n")
        with self.assertRaises(VnTransError):
            script.rebuild({script.units[0].id: "@jump target=*x"})

    def test_wrapping_is_control_aware(self):
        unit = make_unit("test", "dialogue", "One [font color=red]two[resetfont] three", "ks")
        result = render(unit, unit.source, "ks", 7)
        self.assertEqual(result, "One [font color=red]two[resetfont][r]three")
        self.assertEqual(width("a\u0301界"), 3)
        unit = make_unit("test", "dialogue", "source", "ks")
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            self.assertEqual(render(unit, "unbreakable", "ks", 3), "unbreakable")
        self.assertEqual(len(records), 1)
