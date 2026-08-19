import json
import tempfile
import unittest
from pathlib import Path

from webapp.evidence_router import EvidenceRouter, extract_evidence_constraints
from webapp.knowledge_bases import KnowledgeBaseCatalog
from webapp.settings import Settings


class EvidenceRouterTest(unittest.TestCase):
    def make_router(self, directory: str, chunks: dict[str, str]) -> EvidenceRouter:
        root = Path(directory)
        storage = root / "rag_storage"
        mapping_items = []
        titles = {
            "002": "002_2026年2月全国各地区原保险保费收入情况表_2026年2月全国各地区原保险保费收入情况表.xls",
            "090": "090_2024年9月人身险公司经营情况表_2024年9月人身险公司经营情况表.xlsx",
            "333": "333_2020年7月保险业经营情况表_保险业经营情况表.xls",
        }
        for identifier, content in chunks.items():
            directory_path = storage / identifier
            directory_path.mkdir(parents=True)
            payload = {
                f"chunk-{identifier}": {
                    "content": content,
                    "file_path": f"{identifier}.xlsx",
                }
            }
            (directory_path / "kv_store_text_chunks.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            mapping_items.append((titles[identifier], int(identifier)))
        mapping = root / "name2id.json"
        mapping.write_text(
            json.dumps(dict(mapping_items), ensure_ascii=False),
            encoding="utf-8",
        )
        config = Settings(
            project_root=root,
            name_map_path=mapping,
            storage_root=storage,
            evidence_min_score=8.0,
            evidence_min_margin=1.5,
        )
        return EvidenceRouter(KnowledgeBaseCatalog(config), config)

    def test_extracts_value_metric_and_reporting_basis(self) -> None:
        constraints = extract_evidence_constraints(
            '“原保险保费收入”在“本年累计/截至当期”口径下的数值是34879的是哪个文档'
        )
        self.assertIn("34879", constraints.numbers)
        self.assertIn("原保险保费收入", constraints.phrases)
        self.assertIn("本年累计", constraints.reporting_basis)
        self.assertIn("截至当期", constraints.reporting_basis)

    def test_source_index_is_not_a_value_anchor(self) -> None:
        constraints = extract_evidence_constraints("[来源 1] 原保险保费收入是多少")
        self.assertNotIn("1", constraints.numbers)

    def test_joint_evidence_selects_unique_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(
                directory,
                {
                    "002": "全国各地区原保险保费收入合计为34879，但口径为地区合计。",
                    "090": (
                        "Caption: 2024年9月人身险公司经营情况表。"
                        "项目：原保险保费收入；本年累计/截至当期：34879。"
                    ),
                    "333": "保险业经营情况表中记录了其他年度数据。",
                },
            )
            result = router.route(
                '“原保险保费收入”在“本年累计/截至当期”口径下的数值是34879的是哪个文档'
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.knowledge_base.identifier, "090")
            self.assertIn("34879", result.matched_numbers)

    def test_numeric_only_match_is_not_auto_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(
                directory,
                {
                    "002": "某个无关项目的值为34879。",
                    "090": "原保险保费收入为30062。",
                },
            )
            result = router.route(
                '“原保险保费收入”在“本年累计/截至当期”口径下的数值是34879的是哪个文档'
            )
            self.assertIsNone(result)

    def test_equal_supported_candidates_remain_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common = "原保险保费收入；本年累计/截至当期：34879。"
            router = self.make_router(directory, {"002": common, "090": common})
            self.assertIsNone(
                router.route(
                    '“原保险保费收入”在“本年累计/截至当期”口径下的数值是34879的是哪个文档'
                )
            )
            self.assertEqual(len(router.candidates("原保险保费收入 本年累计 34879")), 2)


if __name__ == "__main__":
    unittest.main()
