"""Run local OCR regressions without a Windows server or production credentials."""
import json
import sys
import tempfile
from pathlib import Path
import pymupdf as fitz
from .detector import read_page, assemble


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Uso: python -m app.validate_samples /caminho/amostras.json')
    samples = json.loads(Path(sys.argv[1]).read_text())
    failed = False
    for sample in samples:
        pages = []
        with tempfile.TemporaryDirectory(prefix='iptu-validation-') as temp:
            with fitz.open(sample['path']) as doc:
                for i, page in enumerate(doc):
                    _, decision, rotation = read_page(page, Path(temp), i)
                    pages.append(dict(file_index=0, page=i+1, rotation=rotation, **decision))
        status, selected = assemble(pages)
        actual = [p['page'] for p in selected]
        passed = actual == sample['expected_pages']
        failed |= not passed
        print(json.dumps(dict(sample=sample.get('name', 'amostra'), status=status,
                              expected=sample['expected_pages'], selected=actual, passed=passed),
                         ensure_ascii=False), flush=True)
    raise SystemExit(1 if failed else 0)

if __name__ == '__main__':
    main()
