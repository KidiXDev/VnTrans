import tempfile
import unittest
from binascii import crc_hqx
from pathlib import Path
from struct import pack, pack_into, unpack_from

from vntrans import pipeline
from vntrans.engines import get_engine
from vntrans.model import VnTransError


def checked(data):
    return data + pack(">H", crc_hqx(data, 0))


def nori():
    code = bytearray()
    pool = bytearray()
    for opcode, values in (
        (0x3C, [bytes(range(8)), "忍", "「こんにちは。", "お元気？」"]),
        (0x0B, ["_begin"]),
        (0x66, ["はい"]),
    ):
        count = len(values)
        if opcode == 0x0B:
            label = pack("<II", len(code), 6) + b"_begin"
        flags = count - 1 if opcode == 0x3C else 0xFFFF0000 | count
        code += pack("<HHI", opcode, count, flags)
        for value in values:
            if isinstance(value, str):
                data = value.encode("cp932")
                code += pack("<HHI", 5, len(data), len(pool))
            else:
                data = value
                code += pack("<HHI", 3, 1, len(pool))
            pool += data
    header = b"NORI\0\0\x01\0" + pack(
        "<8I", 1, len(label), 0, 0, 0, 0, len(code), len(pool)
    )
    return (
        checked(header)
        + label[:4]
        + bytes(byte ^ 0x53 for byte in label[4:])
        + checked(label)[len(label) :]
        + bytes(4)
        + checked(code)
        + checked(bytes(byte ^ 0x53 for byte in pool))
    )


def arcc(members):
    header = bytearray(b"ARCC\x02\0\0\0" + bytes(32))
    pack_into("<I", header, 20, len(members))
    names = bytearray()
    files = []
    for index, (name, data) in enumerate(members):
        encoded = bytes(byte ^ 0x69 for byte in name.encode("cp932"))
        names += pack("<HIHH", 0x1FF1, index << 16, len(encoded), 0)
        names += encoded + bytes(2)
        file_header = (
            b"FILE" + pack("<I", 32) + bytes(16) + pack("<I", len(data)) + b"DAEN"
        )
        files.append(checked(file_header) + checked(data))
    index_size = 8 * len(members)
    index = b"NIDX" + bytes(index_size) + b"EIDX" + bytes(index_size) + b"CINF" + names
    addr = 56 + len(index)
    offset = addr + 4 + 12 * len(members)
    addresses = bytearray(b"ADDR")
    for file in files:
        addresses += checked(pack("<HQ", 0x1FF1, offset))
        offset += len(file)
    return (
        checked(header)
        + checked(b"NAME" + pack("<Q", addr))
        + index
        + addresses
        + b"".join(files)
    )


class HexenHausTests(unittest.TestCase):
    def setUp(self):
        self.engine = get_engine("hexenhaus")

    def test_nori_rebuild_updates_later_references_and_preserves_operands(self):
        data = nori()
        script = self.engine.extract("scene.bin", data)
        speaker, dialogue, choice = script.units
        self.assertEqual(
            [unit.kind for unit in script.units], ["speaker", "dialogue", "choice"]
        )
        self.assertEqual(dialogue.source, "「こんにちは。\nお元気？」")
        self.assertEqual(script.rebuild({}), data)
        translated = "“Hello there, how have you been?\nIt’s been such a long time!”"
        rebuilt = script.rebuild({speaker.id: "Shinobu", dialogue.id: translated})
        self.assertGreater(len(rebuilt), len(data))
        parsed = self.engine.extract("scene.bin", rebuilt)
        self.assertEqual(
            [unit.id for unit in parsed.units], [unit.id for unit in script.units]
        )
        self.assertEqual(
            [unit.source for unit in parsed.units],
            ["Shinobu", translated, choice.source],
        )
        pool = bytes(
            byte ^ 0x53 for byte in rebuilt[-2 - unpack_from("<I", rebuilt, 36)[0] : -2]
        )
        self.assertTrue(pool.startswith(bytes(range(8))))
        self.assertIn(b"_begin", pool)
        with self.assertRaisesRegex(VnTransError, r"U\+1F642.*CP932"):
            script.rebuild({choice.id: "Yes 🙂"})

    def test_rebuild_fits_translated_text_to_cp932_and_dialogue_slots(self):
        script = self.engine.extract("scene.bin", nori())
        dialogue, choice = script.units[1:]
        translated = "Tōkyō — café\nSatu – dua\n忍"
        with self.assertWarnsRegex(UserWarning, "NORI line slot"):
            rebuilt = script.rebuild({dialogue.id: translated})
        parsed = self.engine.extract("scene.bin", rebuilt)
        self.assertEqual(parsed.units[1].source, "Tokyo ― cafe\nSatu - dua 忍")
        with self.assertRaisesRegex(VnTransError, "line slot"):
            script.rebuild({choice.id: "One\nTwo"})

    def test_wrap_reflows_and_relocates_later_label_when_adding_a_line(self):
        script = self.engine.extract("scene.bin", nori())
        dialogue, choice = script.units[1:]
        values = {dialogue.id: "The sky turned orange and\nwe left."}
        rebuilt = script.rebuild(values, wrap_columns=20)
        self.assertEqual(
            self.engine.extract("scene.bin", rebuilt).units[1].source,
            "The sky turned\norange and we left.",
        )
        expanded = script.rebuild(values, wrap_columns=16)
        parsed = self.engine.extract("scene.bin", expanded)
        self.assertEqual(parsed.units[1].source, "The sky turned\norange and we\nleft.")
        self.assertEqual(parsed.units[2].source, choice.source)
        old_offset = unpack_from("<I", rebuilt, 42)[0]
        new_offset = unpack_from("<I", expanded, 42)[0]
        self.assertEqual(new_offset, old_offset + 8)
        self.assertEqual(
            unpack_from("<H", expanded, parsed.code_start + new_offset)[0], 0x0B
        )
        self.assertEqual(
            expanded[56:58], checked(pack("<II", new_offset, 6) + b"_begin")[-2:]
        )
        with self.assertWarnsRegex(UserWarning, "needs 12 columns.*target is 4"):
            script.rebuild({dialogue.id: "unbreakable! words"}, wrap_columns=4)

    def test_archive_pipeline_relocates_and_preserves_untouched_member(self):
        asset = b"untouched resource"
        original = arcc([("scene.bin", nori()), ("defaults.var", asset)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rio.arc"
            source.write_bytes(original)
            docs = root / "json"
            self.assertEqual(
                pipeline.extract(source, output=docs, engine="hexenhaus").units, 3
            )
            doc_path = docs / "rio.arc.json"
            doc = pipeline.read_document(doc_path)
            doc["units"][-1]["translation"] = "Of course I will"
            doc_path.write_bytes(pipeline.encode_document(doc))
            pipeline.validate(source, translations=docs)
            pipeline.rebuild(source, translations=docs, output=root / "patch")
            patch = (root / "patch" / "rio.arc").read_bytes()

        result = self.engine.extract("rio.arc", patch)
        self.assertEqual(result.units[-1].source, "Of course I will")
        self.assertEqual(self.engine.extract("rio.arc", original).rebuild({}), original)
        addr = unpack_from("<Q", patch, 46)[0]
        offset = unpack_from("<Q", patch, addr + 18)[0]
        self.assertGreater(offset, unpack_from("<Q", original, addr + 18)[0])
        self.assertEqual(patch[offset + 34 : -2], asset)
        with self.assertRaisesRegex(VnTransError, "checksum"):
            self.engine.extract("rio.arc", patch[:-1] + bytes([patch[-1] ^ 1]))
