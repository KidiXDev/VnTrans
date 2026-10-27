# Source references and attribution

VnTrans reimplements Kirikiri extraction and rebuilding in Python. It does not
bundle or invoke the reference tools at runtime.

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
