"""OCR every page and compare with the PDF text layer.

    python -m scripts.ocr_check data/ct200_manual.pdf
"""
import sys

from app.parser import ocr_crosscheck

if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(path)
        for r in ocr_crosscheck(path):
            print(f"  page {r['page']}: {r['ocr_words']} OCR words, coverage {r['coverage']:.1%}, "
                  f"OCR-only: {r['ocr_only_words']}")
