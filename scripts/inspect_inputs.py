from __future__ import annotations

from pathlib import Path

import pandas as pd
from docx import Document


def main() -> None:
    root = Path(__file__).resolve().parents[1]

    doc_path = root / "Brand Guidelines.docx"
    xls_path = root / "TotaliPhoneRentals.xls"

    # --- DOCX ---
    doc = Document(str(doc_path))
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    print("DOCX_PARAS:", len(paras))
    print("--- DOCX PREVIEW (first 80 paragraphs) ---")
    for p in paras[:80]:
        print(p)

    # --- XLS ---
    df = pd.read_excel(xls_path, engine="xlrd")
    print("\nXLS_SHAPE:", df.shape)
    print("COLUMNS:")
    for c in df.columns:
        print("-", repr(c))

    print("\nSAMPLE_ROWS (first 10):")
    with pd.option_context("display.max_columns", 200, "display.width", 200):
        print(df.head(10).to_string(index=False))

    keywords = ["phone", "rental", "rent", "license", "licence", "scan", "lead", "device"]
    print("\nCOLUMN_KEYWORDS_MATCHES:")
    for c in df.columns:
        s = str(c).lower()
        if any(k in s for k in keywords):
            print("*", c)


if __name__ == "__main__":
    main()
