import json
import tempfile
import unittest
from pathlib import Path

from webapp.knowledge_bases import KnowledgeBaseCatalog
from webapp.output_pdf_catalog import OutputPdfCatalog, OutputPdfCatalogError
from webapp.settings import Settings


class OutputPdfCatalogTest(unittest.TestCase):
    def make_catalog(self, directory: str) -> OutputPdfCatalog:
        root = Path(directory)
        storage = root / "rag_storage"
        output = root / "output"
        storage.mkdir()
        output.mkdir()
        mapping: dict[str, int] = {}
        for value in (1, 2, 3, 4):
            identifier = f"{value:03d}"
            (storage / identifier).mkdir()
            (output / identifier).mkdir()
            mapping[f"{identifier}_document.xls"] = value
        (output / "001" / "001.pdf").write_bytes(b"direct")
        origin = output / "002" / "job" / "002" / "hybrid_auto"
        origin.mkdir(parents=True)
        (origin / "002_origin.pdf").write_bytes(b"origin")
        layout = output / "003" / "job" / "003" / "hybrid_auto"
        layout.mkdir(parents=True)
        (layout / "003_layout.pdf").write_bytes(b"layout")
        mapping_path = root / "name2id.json"
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
        config = Settings(
            project_root=root,
            name_map_path=mapping_path,
            storage_root=storage,
            output_root=output,
        )
        return OutputPdfCatalog(config, KnowledgeBaseCatalog(config))

    def test_direct_origin_layout_and_missing_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            self.assertEqual(catalog.resolve("001").selection_method, "direct")
            self.assertEqual(catalog.resolve("002").selection_method, "origin")
            self.assertEqual(catalog.resolve("003").selection_method, "layout")
            self.assertIsNone(catalog.resolve("004"))

    def test_rejects_invalid_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            for value in ("", "abc", "../001", "1000", "999"):
                with self.subTest(value=value):
                    with self.assertRaises(OutputPdfCatalogError):
                        catalog.resolve(value)
