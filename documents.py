"""Document generation: NDA .docx, personalisation, PDF conversion and wordmarking."""
import io, os, re, shutil, subprocess, tempfile, hashlib
from datetime import date
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color
import config

BASE = os.path.dirname(os.path.abspath(__file__))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _set_header_footer(doc, prospect, ref, header=True):
    for s in doc.sections:
        s.header.is_linked_to_previous = False
        s.footer.is_linked_to_previous = False
        if not header:
            fp = s.footer.add_paragraph()
            r = fp.add_run(f"STRICTLY PRIVATE & CONFIDENTIAL — Prepared exclusively for {prospect}")
            r.font.size = Pt(7); r.bold = True; r.font.color.rgb = RGBColor(0x80, 0, 0)
            fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        else:
          hp = s.header.paragraphs[0] if s.header.paragraphs else s.header.add_paragraph()
          r = hp.add_run(f"STRICTLY PRIVATE & CONFIDENTIAL — Prepared exclusively for {prospect}")
          r.font.size = Pt(8); r.bold = True; r.font.color.rgb = RGBColor(0x80, 0, 0)
          hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp = s.footer.add_paragraph()
        r = fp.add_run(f"Ref {ref} · Issued {date.today():%d %B %Y} to {prospect} · Not for onward distribution")
        r.font.size = Pt(7); r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _replace_in_paragraph(p, mapping):
    full = "".join(r.text for r in p.runs)
    if not any(k in full for k in mapping):
        return
    for k, v in mapping.items():
        full = full.replace(k, v)
    for i, r in enumerate(p.runs):
        r.text = full if i == 0 else ""


def _iter_paragraphs(doc):
    yield from doc.paragraphs
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for s in doc.sections:
        for part in (s.header, s.footer):
            yield from part.paragraphs


def personalise_docx(src, dst, prospect, ref, extra=None):
    """Replace {{PROSPECT}}, {{DATE}}, {{REF}} placeholders and stamp header/footer."""
    doc = Document(src)
    mapping = {"{{PROSPECT}}": prospect, "{{DATE}}": f"{date.today():%d %B %Y}", "{{REF}}": ref}
    mapping.update(extra or {})
    for p in _iter_paragraphs(doc):
        _replace_in_paragraph(p, mapping)
    _set_header_footer(doc, prospect, ref)
    doc.core_properties.title = f"Prepared for {prospect}"
    doc.core_properties.comments = f"Ref {ref}"
    doc.save(dst)


def docx_to_pdf(src, outdir):
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
                        "--convert-to", "pdf", "--outdir", outdir, src],
                       check=True, capture_output=True, timeout=180)
    return os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")


def _overlay(w, h, prospect, ref, logo=None, brand=True):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    # diagonal watermark
    c.saveState()
    c.setFillColor(Color(0.6, 0.6, 0.6, alpha=0.15))
    c.translate(w / 2, h / 2); c.rotate(45)
    c.setFont("Helvetica-Bold", 44); c.drawCentredString(0, 20, config.WORDMARK_TEXT)
    c.setFont("Helvetica", 20); c.drawCentredString(0, -20, f"Prepared for {prospect}")
    c.restoreState()
    # top-right wordmark
    if not brand:
        pass
    elif logo and os.path.exists(logo):
        c.drawImage(logo, w - 62, h - 62, width=40, height=40, preserveAspectRatio=True, mask="auto")
    else:
        c.setFillColor(Color(0.1, 0.15, 0.3, alpha=0.9)); c.setFont("Helvetica-Bold", 9)
        c.drawRightString(w - 30, h - 25, config.WORDMARK_TEXT)
    c.setFillColor(Color(0.4, 0.4, 0.4)); c.setFont("Helvetica", 6.5)
    c.drawCentredString(w / 2, 12, f"{config.FIRM_NAME} · Ref {ref} · Confidential to {prospect}")
    c.save(); buf.seek(0)
    return PdfReader(buf).pages[0]


def wordmark_pdf(src, dst, prospect, ref, append=None, password=None, brand=True):
    writer = PdfWriter(clone_from=src)
    logo = os.path.join(BASE, "data", "assets", "wordmark.png")
    for page in writer.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        page.merge_page(_overlay(w, h, prospect, ref, logo, brand))
    if append and os.path.exists(append):     # appended unmodified (e.g. OneNDA terms, CC BY-ND)
        writer.append(append)
    writer.add_metadata({"/Title": f"Prepared for {prospect}", "/Subject": f"Ref {ref}",
                         "/Author": config.FIRM_NAME})
    if password:
        writer.encrypt(user_password=password, owner_password=config.SECRET_KEY, permissions_flag=0b0100)
    with open(dst, "wb") as f:
        writer.write(f)


# ---------------------------------------------------------------- oneNDA v2.1
ONENDA_TEMPLATE = os.path.join(BASE, "data", "assets", "onenda_v2.1.docx")


def _fill(cell, text, para=0, keep_label=True):
    """Write text after the label in a template cell (keeps template formatting)."""
    p = cell.paragraphs[para]
    if keep_label and p.runs:
        p.add_run(text)
    else:
        for r in p.runs[1:]: r.text = ""
        if p.runs: p.runs[0].text = text
        else: p.add_run(text)


def _add_logo_banner(d):
    """Demeisen logo (left) and oneNDA logo (right) at the top of page 1."""
    from docx.shared import Cm
    from docx.enum.text import WD_TAB_ALIGNMENT
    sec = d.sections[0]
    width = sec.page_width - sec.left_margin - sec.right_margin
    first = d.paragraphs[0]
    p = first.insert_paragraph_before()
    p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    p.paragraph_format.space_after = Pt(12)
    ours = os.path.join(BASE, "data", "assets", "wordmark.png")
    onenda = os.path.join(BASE, "data", "assets", "onenda_logo.png")
    if os.path.exists(ours): p.add_run().add_picture(ours, height=Cm(1.6))
    p.add_run("\t")
    if os.path.exists(onenda): p.add_run().add_picture(onenda, height=Cm(1.0))


def build_nda_docx(nda, dst):
    """Fill the variables of the official oneNDA v2.1 template (CC BY 4.0).
    Only the Parties/Variables tables are touched; the Terms are left as published."""
    d = Document(ONENDA_TEMPLATE)
    # drop the drafting guidance note under "PARTIES AND EXECUTION"
    for p in list(d.paragraphs):
        if p.text.startswith("Guidance:"):
            p._element.getparent().remove(p._element)
    _add_logo_banner(d)
    parties, var = d.tables[0], d.tables[1]
    ours = (f"{config.FIRM_NAME}, a limited liability partnership registered in England and Wales "
            f"(no. {config.FIRM_COMPANY_NO}) whose registered office is at {config.FIRM_ADDRESS} "
            f"and whose principal place of business is at {config.FIRM_OPERATING_ADDRESS}")
    cells = parties.rows
    _fill(cells[1].cells[0], ours, para=1, keep_label=False)
    _fill(cells[1].cells[1], nda["counterparty"] + (f", {nda['cp_details']}" if nda["cp_details"] else ""),
          para=1, keep_label=False)
    for row, (a, b) in zip((3, 4, 5), ((nda["our_signatory"], nda["cp_signatory"]),
                                       (nda["our_title"], nda["cp_signatory_title"]),
                                       (nda["our_email"], nda["cp_email"]))):
        _fill(cells[row].cells[0], a or ""); _fill(cells[row].cells[1], b or "")
    _fill(var.cell(0, 1), nda["purpose"], keep_label=False)
    _fill(var.cell(1, 1), nda["conf_period"], keep_label=False)
    _fill(var.cell(2, 1), nda["governing_law"], keep_label=False)
    dr = var.cell(3, 1)
    _fill(dr, nda["dispute"], keep_label=False)
    for p in dr.paragraphs[1:]: p._element.getparent().remove(p._element)
    sp = var.cell(4, 1)
    _fill(sp, (nda["changes"] or "None.").strip(), para=1, keep_label=False)
    for p in sp.paragraphs[1:]:
        for r in p.runs: r.font.highlight_color = None
    _set_header_footer(d, nda["counterparty"], nda["ref"], header=False)
    d.core_properties.title = f"Mutual NDA - {config.FIRM_NAME} / {nda['counterparty']}"
    d.save(dst)
