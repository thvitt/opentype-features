import sys
import argparse
import os
from fontTools.ttLib import TTFont
from fpdf import FPDF

def get_tag_name(tag):
    return tag

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze OpenType features in a font file.")
    parser.add_argument("font_path", help="Path to the .ttf or .otf font file")
    parser.add_argument("--output", "-o", help="Path to save the PDF report (optional)")
    args = parser.parse_args()

    font_path = args.font_path
    if not os.path.exists(font_path):
        print(f"Error: File not found: {font_path}")
        sys.exit(1)

    try:
        font = TTFont(font_path)
    except Exception as e:
        print(f"Error loading font: {e}")
        sys.exit(1)

    if 'GSUB' not in font:
        print("No GSUB table found. This font might not have OpenType features.")
        return

    gsub = font['GSUB'].table
    feature_list = gsub.FeatureList.FeatureRecord
    lookup_list = gsub.LookupList.Lookup
    
    glyph_names = font.getGlyphOrder()
    cmap = font.getBestCmap()
    reverse_cmap = {v: k for k, v in cmap.items()}

    def glyph_to_chars(glyph_name):
        if glyph_name in reverse_cmap:
            return chr(reverse_cmap[glyph_name])
        return None # Return None if no direct unicode mapping

    def format_glyph(glyph_name):
        c = glyph_to_chars(glyph_name)
        return c if c else f"<{glyph_name}>"

    print(f"Analyzing font: {font_path}")
    print("-" * 40)

    pdf = None
    if args.output:
        pdf = FPDF()
        pdf.add_page()
        try:
            pdf.add_font("TargetFont", style="", fname=font_path)
            # Use TargetFont for headers too to avoid encoding issues
            pdf.set_font("TargetFont", "", 16)
            pdf.cell(0, 10, f"OpenType Feature Report: {os.path.basename(font_path)}", ln=True)
            pdf.ln(5)
        except Exception as e:
            print(f"Warning: Could not embed font for PDF: {e}")
            pdf = None

    def process_subtable(lookup_type, subtable, tag):
        substitutions = []
        if lookup_type == 1: # Single
            if hasattr(subtable, 'mapping'):
                for input_glyph, output_glyph in subtable.mapping.items():
                    in_char = glyph_to_chars(input_glyph)
                    out_char = glyph_to_chars(output_glyph)
                    if in_char:
                        substitutions.append((in_char, out_char if out_char else f"<{output_glyph}>", tag))
        
        elif lookup_type == 2: # Multiple
            if hasattr(subtable, 'mapping'):
                for input_glyph, output_glyphs in subtable.mapping.items():
                    in_char = glyph_to_chars(input_glyph)
                    if in_char:
                        out_chars = "".join(glyph_to_chars(g) if glyph_to_chars(g) else f"<{g}>" for g in output_glyphs)
                        substitutions.append((in_char, out_chars, tag))

        elif lookup_type == 4: # Ligature
            if hasattr(subtable, 'ligatures'):
                for first_glyph, ligatures in subtable.ligatures.items():
                    for lig in ligatures:
                        components = [first_glyph] + lig.Component
                        output = lig.LigGlyph
                        comp_chars = "".join(glyph_to_chars(c) if glyph_to_chars(c) else f"<{c}>" for c in components)
                        out_char = glyph_to_chars(output)
                        if comp_chars:
                            substitutions.append((comp_chars, out_char if out_char else f"<{output}>", tag))

        elif lookup_type == 3: # Alternate
            if hasattr(subtable, 'alternates'):
                for input_glyph, alternates in subtable.alternates.items():
                    in_char = glyph_to_chars(input_glyph)
                    if in_char:
                        out_char = glyph_to_chars(alternates[0])
                        substitutions.append((in_char, out_char if out_char else f"<{alternates[0]}>", tag))
        return substitutions

    def process_lookup(lookup, tag):
        substitutions = []
        if lookup.LookupType == 7: # Extension
            for subtable in lookup.SubTable:
                ext_lookup_type = subtable.ExtensionLookupType
                actual_subtable = subtable.ExtSubTable
                substitutions.extend(process_subtable(ext_lookup_type, actual_subtable, tag))
        else:
            for subtable in lookup.SubTable:
                substitutions.extend(process_subtable(lookup.LookupType, subtable, tag))
        return substitutions

    # Collect all substitutions per tag
    feature_substitutions = {}
    for feature_record in feature_list:
        tag = feature_record.FeatureTag
        if tag not in feature_substitutions:
            feature_substitutions[tag] = []
        
        for lookup_index in feature_record.Feature.LookupListIndex:
            if lookup_index < len(lookup_list):
                lookup = lookup_list[lookup_index]
                feature_substitutions[tag].extend(process_lookup(lookup, tag))

    for tag in sorted(feature_substitutions.keys()):
        all_substitutions = feature_substitutions[tag]
        
        # Deduplicate substitutions
        unique_subs = []
        seen = set()
        for s in all_substitutions:
            if s not in seen:
                unique_subs.append(s)
                seen.add(s)
        all_substitutions = unique_subs

        if all_substitutions:
            print(f"\nFeature: {tag}")
            for in_txt, out_txt, feat_tag in all_substitutions:
                print(f"  {in_txt} -> {out_txt}")

        if pdf:
            pdf.set_font("TargetFont", "", 12)
            pdf.cell(0, 10, f"Feature: {tag}", ln=True)
            pdf.set_font("TargetFont", "", 10)
            if all_substitutions:
                pdf.cell(40, 8, "Input (OFF)", border=1)
                pdf.cell(40, 8, "Output (ON)", border=1)
                pdf.cell(40, 8, "Actual Rendering", border=1)
                pdf.ln()

                for in_txt, out_txt, feat_tag in all_substitutions:
                    try:
                        pdf.cell(40, 10, in_txt, border=1)
                        pdf.cell(40, 10, out_txt if out_txt != in_txt else "N/A", border=1)
                        pdf.cell(40, 10, out_txt, border=1)
                        pdf.ln()
                    except Exception as e:
                        pass
                pdf.ln(5)
            else:
                pdf.cell(0, 10, "No simple substitutions found for this feature.", ln=True)
                pdf.ln()

    if pdf and args.output:
        try:
            pdf.output(args.output)
            print(f"\nPDF report saved to: {args.output}")
        except Exception as e:
            print(f"Error saving PDF: {e}")

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
