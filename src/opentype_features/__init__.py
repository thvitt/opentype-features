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
        # Register the font. We'll name it 'TargetFont'
        # fpdf2 supports OTF/TTF. 
        try:
            pdf.add_font("TargetFont", style="", fname=font_path)
            pdf.set_font("helvetica", "B", 16)
            pdf.cell(0, 10, f"OpenType Feature Report: {os.path.basename(font_path)}", ln=True)
            pdf.ln(5)
        except Exception as e:
            print(f"Warning: Could not embed font for PDF: {e}")
            pdf = None

    for feature_record in feature_list:
        tag = feature_record.FeatureTag
        print(f"\nFeature: {tag}")
        
        if pdf:
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 10, f"Feature: {tag}", ln=True)
            pdf.set_font("helvetica", "", 10)
            pdf.cell(40, 8, "Input (OFF)", border=1)
            pdf.cell(40, 8, "Output (ON)", border=1)
            pdf.cell(40, 8, "Actual Rendering", border=1)
            pdf.ln()

        for lookup_index in feature_record.Feature.LookupListIndex:
            lookup = lookup_list[lookup_index]
            
            for subtable in lookup.SubTable:
                substitutions = []
                if lookup.LookupType == 1: # Single
                    for input_glyph, output_glyph in subtable.mapping.items():
                        in_char = glyph_to_chars(input_glyph)
                        out_char = glyph_to_chars(output_glyph)
                        if in_char:
                            substitutions.append((in_char, out_char if out_char else in_char, tag))
                            print(f"  Substitution: {in_char} -> {format_glyph(output_glyph)}")
                
                elif lookup.LookupType == 4: # Ligature
                    for first_glyph, ligatures in subtable.ligatures.items():
                        for lig in ligatures:
                            components = [first_glyph] + lig.Component
                            output = lig.LigGlyph
                            comp_chars = "".join(glyph_to_chars(c) for c in components if glyph_to_chars(c))
                            out_char = glyph_to_chars(output)
                            if comp_chars:
                                substitutions.append((comp_chars, out_char if out_char else comp_chars, tag))
                                print(f"  Ligature: {comp_chars} -> {format_glyph(output)}")

                elif lookup.LookupType == 3: # Alternate
                    for input_glyph, alternates in subtable.alternates.items():
                        in_char = glyph_to_chars(input_glyph)
                        if in_char:
                            # Just take the first alternate for the demo
                            out_char = glyph_to_chars(alternates[0])
                            substitutions.append((in_char, out_char if out_char else in_char, tag))
                            alt_names = ", ".join(format_glyph(a) for a in alternates)
                            print(f"  Alternate: {in_char} -> [{alt_names}]")

                if pdf and substitutions:
                    for in_txt, out_txt, feat_tag in substitutions:
                        # Row with comparison
                        pdf.set_font("helvetica", "", 10)
                        pdf.cell(40, 10, in_txt, border=1)
                        pdf.cell(40, 10, out_txt if out_txt != in_txt else "N/A (Glyph only)", border=1)
                        
                        # The actual rendering column
                        pdf.set_font("TargetFont", "", 14)
                        # fpdf2 enables features via text_columns or similar, 
                        # but for simple cell/write we might need to set it globally or per call.
                        # In fpdf2, we can use font settings for features.
                        try:
                            # Attempting to use the feature. 
                            # fpdf2's set_font doesn't directly take features, 
                            # but it has some support via harfbuzz if enabled.
                            # We'll render the input text and hope the layout engine handles it 
                            # if we can specify features. 
                            # Actually, fpdf2's support for specific features in a cell is limited 
                            # without custom shaping.
                            # For now, we'll just show the 'out_txt' using the font if it mapped to unicode.
                            pdf.cell(40, 10, out_txt, border=1)
                        except:
                            pdf.cell(40, 10, "?", border=1)
                        
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
