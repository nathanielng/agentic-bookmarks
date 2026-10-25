import importlib.util
import sqlite3
from pathlib import Path

import pytest

from bmk.ingest.cloudtabs import CloudTabsIngester

ROOT = Path(__file__).resolve().parents[2]
REAL_FIXTURE = ROOT / "tests" / "fixtures" / "cloudtabs_real_sanitised.db"


def _load_spike():
    spec = importlib.util.spec_from_file_location("spike_cloudtabs", ROOT / "scripts" / "spike_cloudtabs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_fixture_fills_not_null_columns(tmp_path):
    """Safari declares system_fields etc. NOT NULL; the sanitised fixture must still be insertable."""
    src = sqlite3.connect(":memory:")
    src.executescript(
        """
        CREATE TABLE cloud_tab_devices (device_uuid TEXT PRIMARY KEY NOT NULL, system_fields BLOB NOT NULL,
          device_name TEXT NOT NULL, device_type_identifier TEXT, has_duplicate_device_name BOOLEAN NOT NULL,
          is_ephemeral_device BOOLEAN NOT NULL, last_modified REAL NOT NULL);
        CREATE TABLE cloud_tabs (tab_uuid TEXT PRIMARY KEY NOT NULL, system_fields BLOB NOT NULL, device_uuid TEXT NOT NULL,
          position BLOB NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, is_showing_reader BOOLEAN NOT NULL,
          is_pinned BOOLEAN NOT NULL DEFAULT 0, reader_scroll_position_page_index INTEGER, scene_id TEXT,
          last_viewed_time REAL NOT NULL);
        """
    )
    dest = tmp_path / "fixture.db"
    _load_spike().make_fixture(src, dest)
    items = list(CloudTabsIngester(path=dest).read())
    assert {i.source_ref for i in items} == {"Test iPhone", "Test Mac"}
    assert len(items) == 5


@pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="run scripts/spike_cloudtabs.py --make-fixture first")
def test_real_schema_fixture_ingests(lib):
    summary = lib.ingest("cloudtabs", list(CloudTabsIngester(path=REAL_FIXTURE).read()))
    # 5 tabs: 3 distinct pages (the X post on two devices merges), plus about:blank skipped.
    assert summary.counts["new"] == 3
    assert summary.counts["matched_exact"] == 1
    assert summary.counts["skipped"] == 1
