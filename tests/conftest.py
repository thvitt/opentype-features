from typing import TYPE_CHECKING

import pytest
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

if TYPE_CHECKING:
    from pathlib import Path

GLYPH_ORDER = [
    ".notdef",
    "space",
    "a",
    "b",
    "c",
    "d",
    "f",
    "i",
    "a.sc",
    "a.sc.alt",
    "b.alt",
    "c.alt",
    "d.alt1",
    "d.alt2",
    "f_i",
    "i.TRK",
    "A",
    "B",
    "warning",
]

CMAP = {ord(name): name for name in "abcdfiAB"} | {ord(" "): "space"}

FEATURES = """
languagesystem DFLT dflt;
languagesystem latn dflt;
languagesystem latn TRK;

lookup ALT { sub b by b.alt; sub c by c.alt; } ALT;
lookup SC { sub a by a.sc; } SC;
lookup LIG { sub f i by f_i; } LIG;

feature liga { sub f i by f_i; } liga;
feature locl { script latn; language TRK exclude_dflt; sub i by i.TRK; } locl;
feature smcp { sub a by a.sc; } smcp;
feature ss01 { sub a.sc by a.sc.alt; } ss01;
feature calt { sub [b c]' lookup ALT a; } calt;
feature clig { sub f' lookup LIG i' a; } clig;
feature dlig { sub b' lookup ALT c' lookup SC lookup ALT; } dlig;
feature cv01 { sub d from [d.alt1 d.alt2]; } cv01;
feature salt { sub b from [b.alt c.alt]; } salt;
feature ss02 { sub [a A] [b B] by warning; } ss02;
"""


def _draw_glyph(index: int) -> object:
    """Draw a box whose position and height are unique to the glyph."""
    pen = TTGlyphPen(None)
    if index > 1:  # .notdef and space stay empty
        left = 50 + (index % 4) * 40
        top = 300 + index * 30
        width = 600 if "_" in GLYPH_ORDER[index] else 300
        pen.moveTo((left, 0))
        pen.lineTo((left, top))
        pen.lineTo((left + width, top))
        pen.lineTo((left + width, 0))
        pen.closePath()
    return pen.glyph()


@pytest.fixture(scope="session")
def test_font(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a small TrueType font exercising the analyzed feature types."""
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(GLYPH_ORDER)
    builder.setupCharacterMap(CMAP)
    builder.setupGlyf({name: _draw_glyph(i) for i, name in enumerate(GLYPH_ORDER)})
    widths = {name: 1000 if "_" in name else 500 for name in GLYPH_ORDER}
    builder.setupHorizontalMetrics({name: (width, 0) for name, width in widths.items()})
    builder.setupHorizontalHeader(ascent=900, descent=-200)
    builder.setupNameTable({"familyName": "FeatureTest", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=900, sTypoDescender=-200, usWinAscent=900)
    builder.setupPost()
    addOpenTypeFeaturesFromString(builder.font, FEATURES)

    path = tmp_path_factory.mktemp("fonts") / "FeatureTest.ttf"
    builder.save(str(path))
    return path
