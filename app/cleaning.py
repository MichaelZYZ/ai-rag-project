"""Conservative, auditable text cleaning before product-document indexing."""
from __future__ import annotations

import re
import unicodedata

# Convert full-width ASCII (including numbers and Latin letters) without changing
# meaningful symbols such as ① or measurement units.
FULLWIDTH_ASCII = str.maketrans({chr(code): chr(code - 0xFEE0)
                                 for code in range(0xFF01, 0xFF5F)})
PAGE_MARKER = re.compile(
    r'^(?:第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页?)?|页码\s*[:：]\s*\d+|page\s+\d+(?:\s+(?:of|/)\s*\d+)?)$',
    re.IGNORECASE,
)


def clean_document(text: str):
    """Return cleaned text and a report; never infer or rewrite product facts."""
    raw_chars = len(text)
    text = unicodedata.normalize('NFC', text).translate(FULLWIDTH_ASCII)
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n')
    text = text.replace('\u3000', ' ').replace('\u00a0', ' ')
    text = text.replace('\ufeff', '').replace('\u200b', '').replace('\u200c', '').replace('\u200d', '')
    # Keep tabs and newlines until each line has been normalized.
    controls = sum(1 for ch in text if unicodedata.category(ch) == 'Cc' and ch not in '\n\t')
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Cc' or ch in '\n\t')
    lines = text.split('\n')
    output = []
    report = {'original_characters': raw_chars, 'input_lines': len(lines),
              'empty_lines_removed': 0, 'page_markers_removed': 0,
              'adjacent_duplicates_removed': 0, 'control_characters_removed': controls,
              'whitespace_normalized_lines': 0}
    for line in lines:
        clean = re.sub(r'[\t ]+', ' ', line).strip()
        if clean != line:
            report['whitespace_normalized_lines'] += 1
        if not clean:
            report['empty_lines_removed'] += 1
            continue
        if PAGE_MARKER.fullmatch(clean):
            report['page_markers_removed'] += 1
            continue
        if output and clean == output[-1] and len(clean) >= 6:
            report['adjacent_duplicates_removed'] += 1
            continue
        output.append(clean)
    report['output_lines'] = len(output)
    result = '\n'.join(output)
    report['cleaned_characters'] = len(result)
    return result, report
