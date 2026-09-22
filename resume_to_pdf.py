#!/usr/bin/env python3
"""Convert markdown resume to PDF."""

import sys
from pathlib import Path

import markdown
from xhtml2pdf import pisa

# xhtml2pdf renders with reportlab's base14 fonts, which only cover the
# WinAnsi/cp1252 character set. En/em dashes are in that set and render fine;
# anything outside it (arrows, bullets, smart quotes, ellipsis) comes out as a
# blank box. Normalize the common ones a resume is likely to contain rather
# than embedding a font just to draw an arrow.
_PDF_SAFE_REPLACEMENTS = {
    "→": "->",  # rightwards arrow
    "←": "<-",  # leftwards arrow
    "•": "-",   # bullet
    "‘": "'", "’": "'",  # smart single quotes
    "“": '"', "”": '"',  # smart double quotes
    "…": "...",  # ellipsis
}


def _pdf_safe(text: str) -> str:
    for char, replacement in _PDF_SAFE_REPLACEMENTS.items():
        text = text.replace(char, replacement)
    return text


def markdown_to_pdf(md_path, pdf_path=None):
    """Convert markdown file to PDF."""
    md_file = Path(md_path)

    if not md_file.exists():
        print(f"Error: {md_path} not found")
        sys.exit(1)

    if pdf_path is None:
        pdf_path = md_file.stem + ".pdf"

    # Read markdown
    md_content = _pdf_safe(md_file.read_text(encoding='utf-8'))

    # Convert to HTML with basic styling
    html_content = markdown.markdown(md_content, extensions=['extra', 'tables'])

    # Wrap in HTML with CSS for better formatting
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.4;
                color: #333;
                margin: 0.75in;
                font-size: 10pt;
            }}
            h1 {{
                font-size: 16pt;
                margin: 0 0 4pt 0;
                font-weight: bold;
            }}
            h2 {{
                font-size: 11pt;
                margin: 10pt 0 4pt 0;
                font-weight: bold;
                border-bottom: 1px solid #999;
                padding-bottom: 2pt;
            }}
            h3 {{
                font-size: 10pt;
                margin: 6pt 0 2pt 0;
                font-weight: bold;
            }}
            h4 {{
                font-size: 10pt;
                margin: 4pt 0 0 0;
                font-weight: bold;
            }}
            p {{
                margin: 4pt 0;
            }}
            ul {{
                margin: 4pt 0;
                padding-left: 20pt;
            }}
            li {{
                margin: 2pt 0;
            }}
            strong {{
                font-weight: bold;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin: 6pt 0;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 4pt;
                text-align: left;
            }}
            th {{
                background-color: #f5f5f5;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """

    # Convert to PDF
    try:
        with open(pdf_path, "wb") as out:
            result = pisa.CreatePDF(full_html, dest=out)
        if result.err:
            print(f"Error creating PDF: {result.err} issue(s) reported")
            sys.exit(1)
        print(f"Created {pdf_path}")
    except Exception as e:
        print(f"Error creating PDF: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resume_to_pdf.py <markdown_file> [output.pdf]")
        print("Example: python resume_to_pdf.py profile/resume.md Resume.pdf")
        sys.exit(1)

    md_file = sys.argv[1]
    pdf_file = sys.argv[2] if len(sys.argv) > 2 else None

    markdown_to_pdf(md_file, pdf_file)
