"""Extract text from the T-APEX and 1080 reference PDFs."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PDFS = [
    r"C:\Users\trigo\OneDrive\Documents\t-apex doc.pdf",
    r"C:\Users\trigo\OneDrive\Documents\1080 doc.pdf",
]

# try several PDF libraries
def try_pypdf(path):
    from pypdf import PdfReader
    r = PdfReader(path)
    out = []
    for i, p in enumerate(r.pages):
        out.append(f"--- PAGE {i+1} of {len(r.pages)} ---")
        out.append(p.extract_text() or "(no text)")
    return "\n".join(out)

def try_pdfplumber(path):
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            out.append(f"--- PAGE {i+1} of {len(pdf.pages)} ---")
            out.append(p.extract_text() or "(no text)")
    return "\n".join(out)

for pdf in PDFS:
    print("=" * 70)
    print(pdf)
    print("=" * 70)
    text = None
    for fn in (try_pypdf, try_pdfplumber):
        try:
            text = fn(pdf)
            print(f"[via {fn.__name__}]")
            break
        except Exception as e:
            print(f"  {fn.__name__} failed: {e}")
    if text:
        print(text[:8000])
    print()
