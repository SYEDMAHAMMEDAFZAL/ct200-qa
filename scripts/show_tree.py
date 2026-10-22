"""Print the parsed tree of a CT-200 PDF (manual inspection aid)."""
import json
import sys

from app.parser import parse_pdf


def main(path: str, verbose: bool = False) -> None:
    doc = parse_pdf(path)
    print(f"TITLE: {doc.title!r}  pages={doc.page_count}  warnings={doc.warnings}")

    def walk(parent, depth):
        for s in doc.children(parent):
            flags = ",".join(a["type"] for a in s.anomalies)
            print(f"{'  ' * depth}[{s.key}] {s.heading}  (level={s.level}, p{s.page}, "
                  f"{len(s.blocks)} blocks, hash={s.content_hash[:8]}) {flags}")
            if verbose:
                for b in s.blocks:
                    print(f"{'  ' * depth}    - {b.kind}: {b.text[:90]!r}")
            walk(s.key, depth + 1)

    walk(None, 0)
    if verbose:
        print(json.dumps([{"key": s.key, "anomalies": s.anomalies, "refs": s.references}
                          for s in doc.sections if s.anomalies or s.references], indent=1))


if __name__ == "__main__":
    main(sys.argv[1], "-v" in sys.argv)
