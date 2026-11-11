# LLP Intranet

Flask + SQLite intranet with four tools:

1. **Mutual NDAs (oneNDA v2.1)** – form → filled oneNDA Word doc + wordmarked PDF, status workflow
   (Draft → Internal review → Sent → Signed by counterparty → Fully executed), executed-copy upload, history.
2. **Prospect-named documents** – upload any .docx; `{{PROSPECT}}`, `{{DATE}}`, `{{REF}}` placeholders are filled,
   and every page gets "Prepared exclusively for <name>" header + reference footer.
3. **Word → PDF with wordmark** – same flow converts to PDF (LibreOffice), adds wordmark, diagonal watermark
   naming the recipient, optional open-password, and logs a SHA-256 in an issue register. `/verify` tells you
   who a forwarded PDF was issued to.
4. **Expenditure memos** – every active LLP member must approve; any rejection blocks it; printable sign-off record.

Everything is recorded in `/audit`.

## Run
```
python3 -m venv .venv && .venv/bin/pip install flask python-docx pypdf reportlab
.venv/bin/python app.py          # http://localhost:5000
```
Requires LibreOffice (`soffice`) for Word→PDF.

## Setup
- Edit `config.py`: firm name/address, wordmark text, **LLP members**, home-page links.
  Members are created on first start with password `changeme` — each should change it (top-right link).
- **oneNDA:** the official oneNDA v2.1 Word template (CC BY 4.0) is in `data/assets/onenda_v2.1.docx`.
  The app fills only the Parties and Variables tables; the Terms and the attribution footer are left as published.
  Variations go in "Special provisions".
- Optional logo: `data/assets/wordmark.png` replaces the text wordmark in the PDF header.

## Before going live
Dev server only. For hosting: set `INTRANET_SECRET`, run under gunicorn behind HTTPS, back up `data/`,
and consider SSO (Microsoft 365) instead of local passwords. For counterparty e-signature, link
DocuSign/Adobe Sign — the current NDA flow tracks status and stores the executed copy.
