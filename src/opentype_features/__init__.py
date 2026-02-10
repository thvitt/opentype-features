import sys
import argparse
import os
from fontTools.ttLib import TTFont
from fpdf import FPDF
from opentype_features.feature_names import FEATURE_NAMES

def get_tag_name(tag):
    return FEATURE_NAMES.get(tag, tag)

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

    # Removed: from fpdf.shaping import ShapingParameters

    def get_glyph_info(glyph_name):
        char = chr(reverse_cmap[glyph_name]) if glyph_name in reverse_cmap else None
        return char, glyph_name

    def get_descriptive_name(glyph_name, substitution_info=None):
        # If it's a generic name, try to make it better
        if glyph_name.startswith('glyph') and glyph_name[5:].isdigit():
            if substitution_info:
                if substitution_info['type'] == 'ligature':
                    return "_".join(substitution_info['components']) + "." + substitution_info['tag']
                elif substitution_info['type'] == 'single':
                    return substitution_info['input'] + "." + substitution_info['tag']
                elif substitution_info['type'] == 'alternate':
                    return substitution_info['input'] + ".alt." + substitution_info['tag']
        return glyph_name

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
                    in_char, in_name = get_glyph_info(input_glyph)
                    out_char, out_name = get_glyph_info(output_glyph)
                    if in_char:
                        # Generate descriptive name for output if generic
                        display_out_name = get_descriptive_name(out_name, {'type': 'single', 'input': in_name, 'tag': tag})
                        substitutions.append({
                            'in_chars': in_char,
                            'in_glyphs': in_name,
                            'out_chars': out_char if out_char else in_char, 
                            'out_glyphs': display_out_name,
                            'tag': tag
                        })
        
        elif lookup_type == 2: # Multiple
            if hasattr(subtable, 'mapping'):
                for input_glyph, output_glyphs in subtable.mapping.items():
                    in_char, in_name = get_glyph_info(input_glyph)
                    if in_char:
                        out_info = [get_glyph_info(g) for g in output_glyphs]
                        out_chars = "".join(c if c else f"<{n}>" for c, n in out_info)
                        out_names = " ".join(n for c, n in out_info)
                        substitutions.append({
                            'in_chars': in_char,
                            'in_glyphs': in_name,
                            'out_chars': out_chars,
                            'out_glyphs': out_names,
                            'tag': tag
                        })

        elif lookup_type == 4: # Ligature
            if hasattr(subtable, 'ligatures'):
                for first_glyph, ligatures in subtable.ligatures.items():
                    for lig in ligatures:
                        components = [first_glyph] + lig.Component
                        output = lig.LigGlyph
                        comp_info = [get_glyph_info(c) for c in components]
                        
                        comp_chars = "".join(c for c, n in comp_info if c)
                        comp_names = [n for c, n in comp_info]
                        out_char, out_name = get_glyph_info(output)
                        
                        if comp_chars:
                            # Generate descriptive name for output if generic
                            display_out_name = get_descriptive_name(out_name, {'type': 'ligature', 'components': comp_names, 'tag': tag})
                            substitutions.append({
                                'in_chars': comp_chars,
                                'in_glyphs': " ".join(comp_names),
                                'out_chars': out_char if out_char else comp_chars,
                                'out_glyphs': display_out_name,
                                'tag': tag
                            })

        elif lookup_type == 3: # Alternate
            if hasattr(subtable, 'alternates'):
                for input_glyph, alternates in subtable.alternates.items():
                    in_char, in_name = get_glyph_info(input_glyph)
                    if in_char:
                        out_char, out_name = get_glyph_info(alternates[0])
                        display_out_name = get_descriptive_name(out_name, {'type': 'alternate', 'input': in_name, 'tag': tag})
                        substitutions.append({
                            'in_chars': in_char,
                            'in_glyphs': in_name,
                            'out_chars': out_char if out_char else in_char,
                            'out_glyphs': display_out_name,
                            'tag': tag
                        })
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
        friendly_name = get_tag_name(tag)
        
        # Deduplicate substitutions
        unique_subs = []
        seen = set()
        for s in all_substitutions:
            # Create a hashable version for the 'seen' set
            s_tuple = (s['in_glyphs'], s['out_glyphs'], s['tag'])
            if s_tuple not in seen:
                unique_subs.append(s)
                seen.add(s_tuple)
        all_substitutions = unique_subs

        if all_substitutions:
            print(f"\nFeature: {tag} ({friendly_name})")
            for s in all_substitutions:
                print(f"  {s['in_glyphs']} ({s['in_chars']}) -> {s['out_glyphs']} ({s['out_chars']})")

        if pdf:
            pdf.set_font("TargetFont", "", 12)
            pdf.cell(0, 10, f"Feature: {tag} ({friendly_name})", ln=True)
            pdf.set_font("TargetFont", "", 10)
            if all_substitutions:
                # Column widths
                w = [45, 35, 35, 45]
                pdf.cell(w[0], 8, "Source Glyphs", border=1)
                pdf.cell(w[1], 8, "OFF (Render)", border=1)
                pdf.cell(w[2], 8, "ON (Render)", border=1)
                pdf.cell(w[3], 8, "Target Glyph(s)", border=1)
                pdf.ln()

                for s in all_substitutions:
                    try:
                        pdf.cell(w[0], 10, s['in_glyphs'], border=1)
                        
                        # OFF Render: Disable shaping or at least ensure no special features
                        pdf.set_text_shaping(use_shaping_engine=False)
                        pdf.cell(w[1], 10, s['in_chars'], border=1)
                        
                        # ON Render: Enable shaping with ONLY this feature
                        # We also enable 'liga' and 'calt' usually, but here we want to 
                        # demonstrate THIS specific feature.
                        pdf.set_text_shaping(use_shaping_engine=True, features={s['tag']: True})
                        pdf.cell(w[2], 10, s['in_chars'], border=1)
                        
                        # Reset for target glyph column
                        pdf.set_text_shaping(use_shaping_engine=False)
                        pdf.cell(w[3], 10, s['out_glyphs'], border=1)
                        pdf.ln()
                    except Exception as e:
                        print(f"Error rendering row: {e}")
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
