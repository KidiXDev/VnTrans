# Source references and attribution

VnTrans reimplements Kirikiri extraction and rebuilding in Python. It does not
bundle or invoke the reference tools at runtime. The optional GUI's bundled icons
and its UI toolkit are listed at the end.

## VNTranslationTools

Source: https://github.com/arcusmaximus/VNTranslationTools

Inspected revision: `36fbe909d02f2e9bb187443e03e2df595f56bfec`.

The Kirikiri SCN scenario field mappings, supported record layouts, and control
code grammar are adapted from `KirikiriScnScript.cs`. KAG conventions were
informed by `KirikiriKsScript.cs`. The Python implementations add occurrence IDs,
validation, source preservation, and JSON exchange rather than porting the full
application.

Copyright (c) 2021 arcusmaximus

## psbfile

Source: https://github.com/number201724/psbfile

The PSB binary layout, name-table traversal, integer arrays, string references,
and container serialization are adapted from its reader/compiler implementation.
VnTrans preserves typed scalar bytes and existing name/string/resource identities
instead of recompiling a general-purpose JSON representation.

Copyright (c) 2015 number201724

## MIT license (applies to both references above)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Other research

FreeMote was consulted as a cross-check of PSB format behavior and header
checksum fields: https://github.com/UlyssesWu/FreeMote . No FreeMote library or
implementation is included or required by VnTrans.

## LinguaGacha

Source: https://github.com/neavo/LinguaGacha

The translation workflow (selective glossary injection, staged prompts, batch
retries, review) was studied as a design reference. No LinguaGacha code, prompt
templates or other assets are included; VnTrans's prompts are original.

## Slint (GUI toolkit, `gui` extra)

Source: https://slint.dev

The optional desktop GUI uses the Slint Python package under the Slint
Royalty-free Desktop, Mobile, and Web Applications License 2.0. As that license
requires, the application shows the Slint attribution (`AboutSlint`) in
Settings. Slint is installed as a dependency and is not bundled in this source
tree.

## Lucide icons

Source: https://lucide.dev (`lucide-static` 1.56.0)

The SVG icons in `src/vntrans/gui/ui/icons/` are from Lucide.

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

Some Lucide icons are derived from Feather (https://feathericons.com),
Copyright (c) 2013-present Cole Bemis, under the MIT license text reproduced above.
