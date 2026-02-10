import sys
import argparse
from fontTools.ttLib import TTFont
from fontTools.otlLib.builder import buildStatTable

def get_tag_name(tag):
    # Common feature tags mapping can be added here if needed
    return tag

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze OpenType features in a font file.")
    parser.add_argument("font_path", help="Path to the .ttf or .otf font file")
    args = parser.parse_args()

    try:
        font = TTFont(args.font_path)
    except Exception as e:
        print(f"Error loading font: {e}")
        sys.exit(1)

    if 'GSUB' not in font:
        print("No GSUB table found. This font might not have OpenType features.")
        return

    gsub = font['GSUB'].table
    feature_list = gsub.FeatureList.FeatureRecord
    lookup_list = gsub.LookupList.Lookup
    
    # Map glyph ID to glyph name for easier reading
    glyph_names = font.getGlyphOrder()
    
    # Attempt to map glyph names to unicode characters
    cmap = font.getBestCmap()
    reverse_cmap = {v: k for k, v in cmap.items()}

    def glyph_to_chars(glyph_name):
        if glyph_name in reverse_cmap:
            return chr(reverse_cmap[glyph_name])
        return f"<{glyph_name}>"

    print(f"Analyzing font: {args.font_path}")
    print("-" * 40)

    for feature_record in feature_list:
        tag = feature_record.FeatureTag
        print(f"\nFeature: {tag}")
        
        for lookup_index in feature_record.Feature.LookupListIndex:
            lookup = lookup_list[lookup_index]
            
            # Simplified analysis of substitutions
            # Lookup types: 1=Single, 4=Ligature, etc.
            for subtable in lookup.SubTable:
                if lookup.LookupType == 1: # Single substitution
                    for input_glyph, output_glyph in subtable.mapping.items():
                        print(f"  Substitution: {glyph_to_chars(input_glyph)} -> {glyph_to_chars(output_glyph)}")
                
                elif lookup.LookupType == 4: # Ligature substitution
                    for first_glyph, ligatures in subtable.ligatures.items():
                        for lig in ligatures:
                            components = [first_glyph] + lig.Component
                            output = lig.LigGlyph
                            comp_chars = "".join(glyph_to_chars(c) for c in components)
                            print(f"  Ligature: {comp_chars} -> {glyph_to_chars(output)}")
                
                # Other lookup types (2=Multiple, 3=Alternate) could be added here
                elif lookup.LookupType == 3: # Alternate substitution
                    for input_glyph, alternates in subtable.alternates.items():
                        alt_chars = ", ".join(glyph_to_chars(a) for a in alternates)
                        print(f"  Alternate: {glyph_to_chars(input_glyph)} -> [{alt_chars}]")

if __name__ == "__main__":
    main()
