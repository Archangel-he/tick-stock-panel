"""移除内置行情源后的路由与网络边界。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api.settings import DataProvidersIn, update_data_providers
from app.data_providers.capabilities import build_capability_matrix
from app.services import preferences
from app.tickflow import client


class NoTickFlowSourceTest(unittest.TestCase):
    def test_old_preferences_fall_back_to_configured_sources(self):
        old = {key: "tickflow" for key in preferences.DEFAULT_DATA_PROVIDERS}
        with patch.object(preferences, "load", return_value=old), patch.object(
            preferences, "_allowed_data_providers", return_value={"fuyao", "stocksdk"},
        ):
            self.assertEqual(preferences.get_daily_data_provider(), "fuyao")
            self.assertEqual(preferences.get_minute_data_provider(), "stocksdk")
            self.assertEqual(preferences.get_full_minute_data_provider(), "none")

    def test_matrix_has_no_removed_source(self):
        sources = [
            {"name": "fuyao", "display_name": "fuyao", "datasets": ["daily", "adj_factor", "realtime", "financial"], "available": True},
            {"name": "stocksdk", "display_name": "stock-sdk", "datasets": ["minute", "depth5"], "available": True},
        ]
        with patch("app.data_providers.capabilities.custom_sources.list_plugins", return_value=sources), patch(
            "app.data_providers.capabilities.custom_sources.list_sources", return_value=[],
        ):
            matrix = build_capability_matrix(preferences.DEFAULT_DATA_PROVIDERS)
        self.assertEqual(sum(cap["usable"] for cap in matrix["capabilities"]), 6)
        self.assertFalse(any(
            candidate["name"] == "tickflow"
            for cap in matrix["capabilities"]
            for candidate in cap["candidates"]
        ))

    def test_removed_source_cannot_be_selected_or_called(self):
        with self.assertRaises(HTTPException) as error:
            update_data_providers(DataProvidersIn(daily_data_provider="tickflow"), SimpleNamespace())
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(RuntimeError):
            client.get_client()
        with self.assertRaises(RuntimeError):
            client.get_async_client()
        self.assertIsNone(client.get_paid_realtime_client())


if __name__ == "__main__":
    unittest.main()
