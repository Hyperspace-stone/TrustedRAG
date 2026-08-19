import hashlib
import unittest

from fastapi.testclient import TestClient

from webapp.app import app
from webapp.settings import settings


class OriginalViewerApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_direct_pdf_metadata_preview_and_original_download(self) -> None:
        metadata_response = self.client.get("/api/corpus/012/metadata")
        self.assertEqual(metadata_response.status_code, 200)
        metadata = metadata_response.json()
        self.assertEqual(metadata["collection_id"], "012")
        self.assertEqual(metadata["preview_kind"], "pdf")
        self.assertEqual(metadata["preview_selection_method"], "direct")
        self.assertEqual(metadata["preview_file_name"], "012.pdf")
        self.assertEqual(metadata["preferred_file_name"], "012.xls")

        preview = self.client.get(
            "/api/corpus/012/preview",
            headers={"Range": "bytes=0-31"},
        )
        self.assertEqual(preview.status_code, 206)
        self.assertEqual(len(preview.content), 32)
        self.assertTrue(preview.content.startswith(b"%PDF"))
        self.assertTrue(preview.headers["content-range"].startswith("bytes 0-31/"))

        original_path = settings.corpus_root / "012.xls"
        before = hashlib.sha256(original_path.read_bytes()).hexdigest()
        download = self.client.get(
            "/api/corpus/012/download",
            headers={"Range": "bytes=0-15"},
        )
        self.assertEqual(download.status_code, 206)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), before)

    def test_generated_origin_or_layout_fallback(self) -> None:
        metadata = self.client.get("/api/corpus/361/metadata").json()
        self.assertEqual(metadata["preview_kind"], "pdf")
        self.assertIn(metadata["preview_selection_method"], {"origin", "layout"})
        preview = self.client.get(
            "/api/corpus/361/preview",
            headers={"Range": "bytes=0-7"},
        )
        self.assertEqual(preview.status_code, 206)
        self.assertTrue(preview.content.startswith(b"%PDF"))

    def test_collection_461_uses_bounded_xlsx_fallback(self) -> None:
        metadata = self.client.get("/api/corpus/461/metadata").json()
        self.assertEqual(metadata["preview_kind"], "xlsx")
        self.assertEqual(metadata["preview_selection_method"], "xlsx_fallback")
        content = self.client.get("/api/corpus/461/content?limit=20").json()
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["kind"], "xlsx")
        self.assertLessEqual(len(content["rows"]), 20)
        self.assertTrue(content["sheets"])

    def test_duplicate_original_variants_and_safe_failures(self) -> None:
        for identifier in ("394", "395"):
            with self.subTest(identifier=identifier):
                metadata = self.client.get(f"/api/corpus/{identifier}/metadata").json()
                self.assertTrue(metadata["preferred_file_name"].endswith(".xlsx"))
                self.assertEqual(len(metadata["variants"]), 2)

        self.assertEqual(self.client.get("/api/corpus/abc/metadata").status_code, 404)
        invalid_variant = self.client.get(
            "/api/corpus/012/download",
            params={"variant": "../../etc/passwd"},
        )
        self.assertEqual(invalid_variant.status_code, 404)
        invalid_range = self.client.get(
            "/api/corpus/012/preview",
            headers={"Range": "bytes=999999999-"},
        )
        self.assertEqual(invalid_range.status_code, 416)
