"""Dump every text span with font, size and position, plus detected tables (inspection aid)."""
import sys

import pymupdf as fitz
doc = fitz.open(sys.argv[1])
for pno, page in enumerate(doc):
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] != 0: print(pno, "IMG", b["bbox"]); continue
        for l in b["lines"]:
            for s in l["spans"]:
                print(pno, round(s["bbox"][0]), round(s["bbox"][1]), s["font"][:18], round(s["size"],1), s["flags"], repr(s["text"][:70]))
    tabs = page.find_tables()
    for t in tabs: print(pno, "TABLE", [round(x) for x in t.bbox], t.extract()[:2])
