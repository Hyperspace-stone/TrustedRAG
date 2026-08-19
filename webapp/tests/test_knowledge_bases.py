import tempfile
import unittest
from pathlib import Path

from webapp.knowledge_bases import KnowledgeBaseCatalog, KnowledgeBaseError
from webapp.settings import Settings


class KnowledgeBaseCatalogTest(unittest.TestCase):
    def test_catalog_filters_missing_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            storage = tmp_path / "rag_storage"
            (storage / "001").mkdir(parents=True)
            (storage / "100").mkdir(parents=True)
            mapping = tmp_path / "name2id.json"
            mapping.write_text(
                '{"padded.xls":1,"valid.xls":"100","missing.xls":"101","bad.xls":"../bad"}',
                encoding="utf-8",
            )
            config = Settings(project_root=tmp_path, name_map_path=mapping, storage_root=storage)
            catalog = KnowledgeBaseCatalog(config)
            self.assertEqual([item.identifier for item in catalog.list()], ["001", "100"])
            self.assertEqual(catalog.resolve("001").display_name, "padded.xls")
            self.assertEqual(catalog.resolve("100").display_name, "valid.xls")

    def test_catalog_rejects_invalid_or_missing_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            storage = tmp_path / "rag_storage"
            (storage / "100").mkdir(parents=True)
            mapping = tmp_path / "name2id.json"
            mapping.write_text('{"valid.xls":"100"}', encoding="utf-8")
            config = Settings(project_root=tmp_path, name_map_path=mapping, storage_root=storage)
            catalog = KnowledgeBaseCatalog(config)
            for value in ("", "../100", "100/../101", "not-a-number", "101"):
                with self.subTest(value=value):
                    with self.assertRaises(KnowledgeBaseError):
                        catalog.resolve(value)
