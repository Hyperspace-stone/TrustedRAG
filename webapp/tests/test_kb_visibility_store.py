import tempfile
import unittest
from pathlib import Path

from webapp.kb_visibility_store import (
    KnowledgeBaseVisibilityError,
    KnowledgeBaseVisibilityStore,
)


class KnowledgeBaseVisibilityStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = KnowledgeBaseVisibilityStore(
            Path(self.temp_dir.name) / "users.sqlite3"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_existing_collections_to_admin_public(self) -> None:
        record = self.store.get_or_create("2")
        self.assertEqual(record["collection_name"], "002")
        self.assertEqual(record["owner_user_id"], "admin")
        self.assertEqual(record["visibility"], "public")
        self.assertEqual(record["visible_users"], [])

    def test_bulk_load_and_update_shared_users(self) -> None:
        records = self.store.get_many(["1", "002", "1"])
        self.assertEqual(set(records), {"001", "002"})

        updated = self.store.update(
            "001",
            visibility="shared",
            visible_users=["alice", "bob", "alice"],
        )
        self.assertEqual(updated["visibility"], "shared")
        self.assertEqual(updated["visible_users"], ["alice", "bob"])

    def test_private_and_public_clear_visible_users(self) -> None:
        self.store.update("001", visibility="shared", visible_users=["alice"])
        private = self.store.update("001", visibility="private", visible_users=["alice"])
        self.assertEqual(private["visible_users"], [])
        public = self.store.update("001", visibility="public", visible_users=["alice"])
        self.assertEqual(public["visible_users"], [])

    def test_permission_helpers(self) -> None:
        private = self.store.update("001", visibility="private")
        self.assertTrue(self.store.can_manage(private, "admin", True))
        self.assertFalse(self.store.can_manage(private, "alice", False))
        self.assertTrue(self.store.can_view(private, "admin", True))
        self.assertFalse(self.store.can_view(private, "alice", False))

        shared = self.store.update("001", visibility="shared", visible_users=["alice"])
        self.assertTrue(self.store.can_view(shared, "alice", False))
        self.assertFalse(self.store.can_view(shared, "bob", False))

    def test_delete_visibility_record(self) -> None:
        self.store.get_or_create("509")
        self.assertTrue(self.store.delete("509"))
        self.assertFalse(self.store.delete("509"))

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaises(KnowledgeBaseVisibilityError):
            self.store.get_or_create("../001")
        with self.assertRaises(KnowledgeBaseVisibilityError):
            self.store.update("001", visibility="everyone")
        with self.assertRaises(KnowledgeBaseVisibilityError):
            self.store.update("001", visibility="shared", visible_users="alice")


if __name__ == "__main__":
    unittest.main()
