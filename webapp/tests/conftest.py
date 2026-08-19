from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from openpyxl import Workbook


_TEST_ROOT_HANDLE = tempfile.TemporaryDirectory(prefix="trustedrag-tests-")
TEST_ROOT = Path(_TEST_ROOT_HANDLE.name)
DATA_ROOT = TEST_ROOT / "data"


def _write_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% TrustedRAG test fixture\n" + (b"0" * 512))


def _write_xlsx(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"
    worksheet.append(["指标", "本年累计/截至当期"])
    worksheet.append(["原保险保费收入", 34879])
    workbook.save(path)
    workbook.close()


def _prepare_data() -> None:
    storage = DATA_ROOT / "rag_storage"
    corpus = DATA_ROOT / "corpus"
    output = DATA_ROOT / "output"
    pending = corpus / ".pending"
    build = DATA_ROOT / ".trustedrag-builds"
    for directory in (storage, corpus, output, pending, build):
        directory.mkdir(parents=True, exist_ok=True)

    identifiers = ("012", "361", "394", "395", "461")
    mapping: dict[str, str] = {}
    for identifier in identifiers:
        (storage / identifier).mkdir()
        mapping[f"{identifier}_test_document.xlsx"] = str(int(identifier))
    (DATA_ROOT / "name2id.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (corpus / "012.xls").write_bytes(b"test-xls")
    (corpus / "361.xls").write_bytes(b"test-xls")
    for identifier in ("394", "395"):
        (corpus / f"{identifier}.xls").write_bytes(b"old-test-xls")
        _write_xlsx(corpus / f"{identifier}.xlsx")
    _write_xlsx(corpus / "461.xlsx")

    _write_pdf(output / "012" / "012.pdf")
    _write_pdf(output / "361" / "nested" / "361_origin.pdf")


_prepare_data()
os.environ["TRUSTEDRAG_ADMIN_PASSWORD"] = "test123"
os.environ["TRUSTEDRAG_AUTH_DB_PATH"] = str(DATA_ROOT / "trustedrag_auth.sqlite3")
os.environ["TRUSTEDRAG_NAME_MAP_PATH"] = str(DATA_ROOT / "name2id.json")
os.environ["TRUSTEDRAG_STORAGE_ROOT"] = str(DATA_ROOT / "rag_storage")
os.environ["TRUSTEDRAG_CORPUS_ROOT"] = str(DATA_ROOT / "corpus")
os.environ["TRUSTEDRAG_CORPUS_PENDING_ROOT"] = str(DATA_ROOT / "corpus" / ".pending")
os.environ["TRUSTEDRAG_OUTPUT_ROOT"] = str(DATA_ROOT / "output")
os.environ["TRUSTEDRAG_BUILD_ROOT"] = str(DATA_ROOT / ".trustedrag-builds")
os.environ["TRUSTEDRAG_MODEL_SETTINGS_PATH"] = str(DATA_ROOT / "model-settings.json")
