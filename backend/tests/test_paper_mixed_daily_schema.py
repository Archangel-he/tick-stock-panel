"""Daily partitions with optional quote fields must still settle paper orders."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl

from app.strategy import paper


def test_mixed_daily_schema_settles_next_open_order():
    with TemporaryDirectory() as root:
        data_dir = Path(root)
        for day, close, extra in (
            (date(2026, 9, 27), 10.0, False),
            (date(2026, 9, 28), 10.1, True),
            (date(2026, 9, 30), 10.3, False),
        ):
            folder = data_dir / "kline_daily" / f"date={day.isoformat()}"
            folder.mkdir(parents=True)
            row = {"date": [day], "symbol": ["600519.SH"], "open": [10.2], "close": [close]}
            if extra:
                row["quote_ts"] = ["2026-09-28T15:00:00+08:00"]
            pl.DataFrame(row).write_parquet(folder / "part.parquet")

        paper.create_account(data_dir, 100_000)
        order, error = paper.create_order(
            data_dir, "600519.SH", "buy", qty=100,
            order_type="next_open", ref_price=10.1,
        )
        assert error is None
        order["created_at"] = "2026-09-29T15:57:00+08:00"
        paper.save_order(data_dir, order)

        assert paper.settle_day(data_dir, "2026-09-30")["filled"] == 1
        assert paper.get_order(data_dir, order["id"])["status"] == "filled"
        assert paper._prev_close(data_dir, "600519.SH", "stock", "2026-09-30") == 10.1


if __name__ == "__main__":
    test_mixed_daily_schema_settles_next_open_order()
