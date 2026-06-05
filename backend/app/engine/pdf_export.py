"""PDF-Export für den B6-Bilanzbericht.

Nimmt den fertigen `report.md` eines Laufs, konvertiert ihn zu HTML
(mit Markdown-Tabellen-Support) und rendert ihn via weasyprint zu PDF
mit Print-CSS (Seitenzahlen, Header, page-break-Logik).
"""
from __future__ import annotations

import logging
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

log = logging.getLogger("lca.pdf")

_PDF_CSS = """
@page {
  size: A4;
  margin: 22mm 18mm 22mm 18mm;
  @top-right {
    content: "Cluster-Bilanzierung B6";
    font-size: 9pt;
    color: #6b7280;
  }
  @bottom-right {
    content: "Seite " counter(page) " / " counter(pages);
    font-size: 9pt;
    color: #6b7280;
  }
}

html { font-family: "DejaVu Sans", "Helvetica", sans-serif; font-size: 10pt; color: #111827; line-height: 1.45; }
h1 { font-size: 18pt; border-bottom: 2px solid #1f2937; padding-bottom: 4px; margin-top: 0; }
h2 { font-size: 13pt; color: #1f2937; margin-top: 18pt; border-bottom: 1px solid #d1d5db; padding-bottom: 3px; page-break-after: avoid; }
h3 { font-size: 11pt; color: #1f2937; margin-top: 12pt; page-break-after: avoid; }
h4 { font-size: 10pt; color: #374151; margin-top: 10pt; page-break-after: avoid; }
p { margin: 6pt 0; }
ul, ol { margin: 4pt 0 4pt 18pt; padding: 0; }
li { margin: 2pt 0; }
em { color: #4b5563; }
strong { color: #1f2937; }
code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px; font-size: 9pt; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 9pt; page-break-inside: avoid; }
th { background: #f3f4f6; padding: 5pt; text-align: left; border-bottom: 1.5px solid #6b7280; }
td { padding: 4pt 5pt; border-bottom: 1px solid #e5e7eb; }
tr:nth-child(even) td { background: #fafafa; }
hr { border: 0; border-top: 1px solid #d1d5db; margin: 12pt 0; }

/* Diagramme auf Druckbreite begrenzen — sonst läuft das PNG aus dem Seitenrand. */
img { max-width: 100%; height: auto; display: block; margin: 6pt auto; page-break-inside: avoid; }
"""


def render_report_to_pdf(report_md_path: Path, pdf_out_path: Path) -> Path:
    """Konvertiert ein Markdown-Report zu PDF und speichert es."""
    if not report_md_path.is_file():
        raise FileNotFoundError(f"report.md not found at {report_md_path}")

    md_text = report_md_path.read_text(encoding="utf-8")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    body_html = md.convert(md_text)

    html_doc = (
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>"
        "<title>Cluster-Bilanzierung B6</title></head><body>"
        + body_html
        + "</body></html>"
    )

    pdf_out_path.parent.mkdir(parents=True, exist_ok=True)
    # base_url = Verzeichnis der report.md, damit relative ![](chart_*.png)
    # Referenzen aufgelöst werden.
    HTML(string=html_doc, base_url=str(report_md_path.parent)).write_pdf(
        target=str(pdf_out_path),
        stylesheets=[CSS(string=_PDF_CSS)],
    )
    log.info("PDF written to %s (%d bytes)", pdf_out_path, pdf_out_path.stat().st_size)
    return pdf_out_path
