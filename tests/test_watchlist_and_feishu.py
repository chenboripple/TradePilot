"""
观察池联动与飞书通知链路的回归测试。
"""

import tempfile
import unittest
from pathlib import Path

from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
from ripple_tradePilot.storage import user_store


class WatchlistAggregationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "test.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lists_distinct_watched_symbols_across_users(self):
        u1 = user_store.create_user("alice", "pw-alice-1", self.db)
        u2 = user_store.create_user("bob", "pw-bob-123", self.db)
        user_store.add_watchlist_item(u1["id"], "000001.SZ", "平安银行", self.db)
        user_store.add_watchlist_item(u1["id"], "600309.SH", "万华化学", self.db)
        user_store.add_watchlist_item(u2["id"], "000001.SZ", "平安银行", self.db)
        user_store.add_watchlist_item(u2["id"], "002022.SZ", "科华生物", self.db)
        user_store.delete_watchlist_item(u2["id"], "002022.SZ", self.db)  # 移出观察池

        watched = user_store.list_all_watched_symbols(self.db)

        self.assertEqual(
            {item["symbol"] for item in watched},
            {"000001.SZ", "600309.SH"},  # 去重且不含已移出的标的
        )


class FeishuSignatureTest(unittest.TestCase):
    def test_signature_is_deterministic(self):
        notifier = FeishuWebhookNotifier("http://example.com/hook", secret="s3cret")
        self.assertEqual(
            notifier._generate_signature("1700000000"),
            notifier._generate_signature("1700000000"),
        )
        self.assertNotEqual(
            notifier._generate_signature("1700000000"),
            notifier._generate_signature("1700000001"),
        )

    def test_no_signature_without_secret(self):
        notifier = FeishuWebhookNotifier("http://example.com/hook")
        self.assertEqual(notifier._generate_signature("1700000000"), "")


if __name__ == "__main__":
    unittest.main()
