"""Small independent PSB fixtures: deliberately shared strings and mixed types."""

import struct
import zlib


def array(values):
    return b"\x0f" + len(values).to_bytes(3, "little") + b"\x10" + b"".join(
        v.to_bytes(4, "little") for v in values)


def scenario(*, multilingual=False, many_strings=False, resources=True):
    row = ["internal-id", None, "Same text", {"state": -32769}]
    choice = {"text": "Same text", "target": "*next"}
    if multilingual:
        row = ["internal-id", [["JP name", "JP text", "speech", "search"],
                                ["EN name", "EN text", "speech", "search"]]]
        choice["language"] = [{"text": "JP choice"}, {"text": "EN choice"}]
    root = {"name": "scene.ks", "scenes": [{"label": "*start", "title": "Title",
             "texts": [row, [None, None, "Same text"]], "selects": [choice]}],
            "untouched": [None, False, True, 0, -1, 2**40, 1.25, "Same text"]}
    if multilingual:
        root["scenes"][0]["texts"] = [row]
    if many_strings:
        root["zz_extra"] = [f"string-{i}" for i in range(260)]
    if resources:
        root["resource"] = b"resource bytes\0\xff"
    return make_psb(root)


def make_psb(root):
    keys = set()
    def collect(value):
        if isinstance(value, dict):
            keys.update(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(root)
    names = sorted(keys)
    trie = {}
    for name in names:
        node = trie
        for char in name.encode():
            node = node.setdefault(char, {})
        node[None] = name
    base = {}
    parent = {0: 0}
    terminals = {}
    next_block = 1
    def visit(node, state):
        nonlocal next_block
        if None in node:
            terminals[node[None]] = state
        children = [(k, v) for k, v in node.items() if k is not None]
        if children:
            base[state] = next_block
            next_block += 256
            for char, child in children:
                child_state = base[state] + char
                parent[child_state] = state
                visit(child, child_state)
    visit(trie, 0)
    third = []
    for name in names:
        third.append(next_block)
        parent[next_block] = terminals[name]
        next_block += 1
    first = [base.get(i, 0) for i in range(next_block)]
    second = [parent.get(i, 0) for i in range(next_block)]
    name_data = array(first) + array(second) + array(third)
    strings = []
    chunks = []
    def encode(value):
        if isinstance(value, dict):
            members = sorted(value)
            parts = [encode(value[key]) for key in members]
            key_table = array([names.index(key) for key in members])
            tag = b"\x21"
        elif isinstance(value, list):
            parts = [encode(child) for child in value]
            key_table = b""
            tag = b"\x20"
        else:
            if value is None:
                return b"\x01"
            if isinstance(value, bool):
                return b"\x03" if value else b"\x02"
            if isinstance(value, int):
                return b"\x0c" + value.to_bytes(8, "little", signed=True)
            if isinstance(value, float):
                return b"\x1f" + struct.pack("<d", value)
            if isinstance(value, bytes):
                index = len(chunks)
                chunks.append(value)
                return b"\x19" + bytes([index])
            if value not in strings:
                strings.append(value)
            index = strings.index(value)
            return (b"\x15" + bytes([index]) if index < 256 else
                    b"\x16" + index.to_bytes(2, "little"))
        offsets = []
        size = 0
        for part in parts:
            offsets.append(size)
            size += len(part)
        return tag + key_table + array(offsets) + b"".join(parts)
    entries = encode(root)
    parts = []
    offsets = []
    size = 0
    for value in strings:
        part = value.encode() + b"\0"
        offsets.append(size)
        parts.append(part)
        size += len(part)
    string_table = array(offsets)
    strings_data = b"".join(parts)
    chunk_offsets = []
    total = 0
    for part in chunks:
        chunk_offsets.append(total)
        total += len(part)
    resource_offsets = array(chunk_offsets)
    resource_sizes = array([len(part) for part in chunks])
    position = 44
    sections = []
    for section in (name_data, entries, string_table, strings_data,
                    resource_offsets, resource_sizes, b"".join(chunks)):
        sections.append(position)
        position += len(section)
    n, e, s, sd, co, cl, cd = sections
    fields = struct.pack("<8I", 44, n, s, sd, co, cl, cd, e)
    return (b"PSB\0\x03\0\0\0" + fields + struct.pack("<I", zlib.adler32(fields))
            + name_data + entries + string_table + strings_data
            + resource_offsets + resource_sizes + b"".join(chunks))
