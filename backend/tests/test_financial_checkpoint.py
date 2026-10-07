"""长时间财务同步应在每批结束后保存已获取的数据。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import polars as pl

from app.services import financial_sync
from app.tickflow.capabilities import CapabilitySet


class FinancialCheckpointTest(unittest.TestCase):
    def test_completed_batch_survives_later_failure(self):
        symbols = [f"{i:06d}.SZ" for i in range(101)]
        calls = 0

        def fetch(table, chunk, capset, latest_only=True):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("next batch interrupted")
            return pl.DataFrame({
                "symbol": chunk,
                "period_end": ["2026-06-30"] * len(chunk),
                "revenue": [1.0] * len(chunk),
            })

        with tempfile.TemporaryDirectory() as directory, patch.object(
            financial_sync, "_fetch_table", side_effect=fetch,
        ):
            data_dir = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "next batch interrupted"):
                financial_sync._sync_history_table_for_symbols(
                    "income", symbols, data_dir, CapabilitySet(),
                )
            saved = financial_sync.get_financial_df(data_dir, "income")
            self.assertEqual(saved.height, 100)


if __name__ == "__main__":
    unittest.main()
