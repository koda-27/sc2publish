# References and attribution

Native Battle.net framing, BSN bit ordering and connection cryptography were
implemented with reference to [ncarrillo/superiority](https://github.com/ncarrillo/superiority).
The bundled server RSA public key is a public verification key, not a private
key. The schema resource contains protocol type descriptions, not captures,
account settings, game assets, or serialized publication templates.

## superiority — MIT License

Copyright (c) 2026 ncarrillo

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

## Format references

- [MPQ format](http://www.zezula.net/en/mpq/mpqformat.html), Ladislav Zezula.
- [MapInfo format](https://sc2mapster.github.io/mkdocs/data/formats/mapinfo/).

StarCraft II and Battle.net are trademarks of Blizzard Entertainment.
This is an independent community tool. The bundled stock catalog table contains
entry names and ordering extracted from stock game catalogs for interoperability;
it includes no textures, audio, maps or executable game code. The login SRP
implementation was checked against the editor login page's worker using synthetic
inputs; no browser caches or authentication captures are distributed.
