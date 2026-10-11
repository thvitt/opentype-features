# TODO

OpenType functionality the report does not cover yet, roughly by importance.

- **Variable fonts:** report axes (`fvar`), named instances and
  `FeatureVariations` (glyph swaps along an axis, `rvrn`), and render examples
  at several instances.
- **Script and language inventory:** list all script/language systems and show
  language-specific behavior of features other than `locl`.
- **Vertical features** (`vert`, `vrt2`, `vkna`, `vkrn`, …): shape them with
  vertical text direction instead of horizontally.
- **Unicode variation sequences** (cmap format 14), e.g. CJK ideographic
  variants and text/emoji presentation.
- **Colour fonts:** `COLR`/`CPAL` palettes, `SVG `, `sbix`, `CBDT`.
- **Feature parameters:** optical size range of `size`, the character lists and
  sample texts of `cvXX` features.
- **Legacy `kern` table** used instead of GPOS kerning by older fonts.
- **Edge-of-line features** (`rtbd`, `lfbd`, `opbd`): only advance changes of the
  last glyph are invisible in isolation and need a following glyph.
- **Mark-to-ligature and cursive attachment:** fontFeatures cannot unparse
  mark-to-ligature rules yet.
- **Math fonts:** `MATH` table variants and `ssty`.
- **fontFeatures failures:** some fonts (e.g. PragmataPro Mono R and B) fail with
  "Lookups unparsed out of order" and produce no report at all.
- **Classes in ligatures and contexts** still use their first glyph only.
