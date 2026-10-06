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


def _keep_line_breaks(text: str) -> str:
    """Hard-break the lines whose line breaks are meaningful.

    Markdown folds consecutive lines into one paragraph, which runs the
    contact lines under the name together, and a project's title into its link.
    Breaking every line would instead split bullets wherever the source file
    happens to wrap, so only the header (everything before the first ``##``) and
    ``title |`` / link pairs and bold-led lines (project and school names) are
    broken; the rest reflows to the page width.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_header = True
    prev_pipe = False
    for line in lines:
        if line.startswith("## "):
            in_header = False
        ends_pipe = line.rstrip().endswith("|")
        bold_lead = line.startswith("**")
        if (
            line.strip()
            and (in_header or ends_pipe or prev_pipe or bold_lead)
            and not line.startswith("#")
        ):
            line = line.rstrip() + "  "
        prev_pipe = ends_pipe
        out.append(line)
    return "\n".join(out)


def markdown_to_pdf(md_path, pdf_path=None):
    """Convert markdown file to PDF."""
    md_file = Path(md_path)

    if not md_file.exists():
        print(f"Error: {md_path} not found")
        sys.exit(1)

    if pdf_path is None:
        pdf_path = md_file.with_suffix(".pdf")

    # Read markdown
    md_content = _pdf_safe(md_file.read_text(encoding='utf-8'))

    html_content = markdown.markdown(
        _keep_line_breaks(md_content), extensions=['extra', 'tables']
    )

    # Wrap in HTML with CSS for better formatting
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{
                size: letter;
                margin: 0.5in 0.6in;
            }}
            body {{
                font-family: Helvetica, Arial, sans-serif;
                line-height: 1.25;
                color: #222;
                margin: 0;
                font-size: 9.5pt;
            }}
            h1 {{
                font-size: 18pt;
                margin: 0 0 2pt 0;
                font-weight: bold;
            }}
            h2 {{
                font-size: 10.5pt;
                margin: 9pt 0 3pt 0;
                font-weight: bold;
                text-transform: uppercase;
                border-bottom: 1px solid #999;
                padding-bottom: 1pt;
            }}
            h3 {{
                font-size: 9.5pt;
                margin: 6pt 0 2pt 0;
                font-weight: bold;
            }}
            h4 {{
                font-size: 9.5pt;
                margin: 4pt 0 0 0;
                font-weight: bold;
            }}
            p {{
                margin: 2pt 0;
            }}
            ul {{
                margin: 2pt 0 2pt 0;
                padding-left: 0;
                margin-left: 11pt;
            }}
            li {{
                margin: 1.5pt 0;
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
