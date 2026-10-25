import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from bmk import db
from bmk.config import Config
from bmk.ingest.cloudtabs import MAC_EPOCH_OFFSET
from bmk.service import Library

# Columns as found by the Milestone 0 spike on macOS 26.5.2 / Safari 26.5.2 (docs/cloudtabs-schema.md).
# Constraints are relaxed so tests can insert minimal rows; tests/fixtures/cloudtabs_real_sanitised.db
# carries the exact DDL.
CLOUDTABS_SCHEMA = """
CREATE TABLE cloud_tab_devices (device_uuid TEXT PRIMARY KEY, system_fields BLOB, device_name TEXT,
  device_type_identifier TEXT, has_duplicate_device_name BOOLEAN DEFAULT 0, is_ephemeral_device BOOLEAN DEFAULT 0,
  last_modified REAL);
CREATE TABLE cloud_tabs (tab_uuid TEXT PRIMARY KEY, system_fields BLOB, device_uuid TEXT REFERENCES cloud_tab_devices(device_uuid),
  position BLOB, title TEXT, url TEXT NOT NULL, is_showing_reader BOOLEAN DEFAULT 0, is_pinned BOOLEAN DEFAULT 0,
  reader_scroll_position_page_index INTEGER, scene_id TEXT, last_viewed_time REAL);
CREATE TABLE cloud_tab_close_requests (close_request_uuid TEXT PRIMARY KEY, system_fields BLOB, destination_device_uuid TEXT,
  url TEXT, tab_uuid TEXT);
"""

DEVICES = {"DEV-PHONE": "Test iPhone", "DEV-MAC": "Test MacBook"}


class FakeCloudTabs:
    """A CloudTabs.db in WAL mode whose tabs can be changed between polls."""

    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")  # keep writes in the -wal file, like a live Safari
        self.conn.executescript(CLOUDTABS_SCHEMA)
        for uuid, name in DEVICES.items():
            self.conn.execute("INSERT INTO cloud_tab_devices (device_uuid, device_name) VALUES (?, ?)", (uuid, name))

    def open_tab(
        self, tab_uuid: str, url: str, title: str = "", device: str = "DEV-PHONE", last_viewed: datetime | None = None
    ) -> None:
        mac_time = last_viewed.timestamp() - MAC_EPOCH_OFFSET if last_viewed else None
        self.conn.execute(
            "INSERT INTO cloud_tabs (tab_uuid, device_uuid, title, url, last_viewed_time) VALUES (?, ?, ?, ?, ?)",
            (tab_uuid, device, title, url, mac_time),
        )

    def close_tab(self, tab_uuid: str) -> None:
        self.conn.execute("DELETE FROM cloud_tabs WHERE tab_uuid=?", (tab_uuid,))


@pytest.fixture
def cloudtabs(tmp_path) -> FakeCloudTabs:
    fake = FakeCloudTabs(tmp_path / "CloudTabs.db")
    yield fake
    fake.conn.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(db_path=tmp_path / "lib" / "bookmarks.db", cloudtabs_path=None, cloudtabs_devices=[],
                  cloudtabs_exclude_devices=[], resurrect_on_new_save=False, display_tz="Asia/Singapore")


@pytest.fixture
def lib(config) -> Library:
    library = Library(db.connect(config.db_path), config, actor="test")
    yield library
    library.close()
