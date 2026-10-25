# CloudTabs.db findings (Milestone 0 spike)

- Generated: 2026-10-25T06:19:28+00:00
- macOS 26.5.2, Safari 26.5.2
- Path: `~/Library/Containers/com.apple.Safari/Data/Library/Safari/CloudTabs.db`
- Tabs: 485 across 3 devices
- Read via SQLite backup API snapshot (Safari's files untouched)

## Schema

### `cloud_tab_close_requests` (0 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| close_request_uuid | TEXT | 1 | yes | — |
| system_fields | BLOB |  | yes | — |
| destination_device_uuid | TEXT |  | yes | — |
| url | TEXT |  | yes | — |
| tab_uuid | TEXT |  | yes | — |

### `cloud_tab_devices` (3 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| device_uuid | TEXT | 1 | yes | str |
| system_fields | BLOB |  | yes | bytes (1935 bytes, starts 62706c69) |
| device_name | TEXT |  |  | str |
| device_type_identifier | TEXT |  |  | str |
| has_duplicate_device_name | BOOLEAN |  |  | int |
| is_ephemeral_device | BOOLEAN |  |  | int |
| last_modified | REAL |  | yes | float (looks like Mac absolute time: s since 2001) |

### `cloud_tabs` (485 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| tab_uuid | TEXT | 1 | yes | str |
| system_fields | BLOB |  | yes | bytes (1929 bytes, starts 62706c69) |
| device_uuid | TEXT |  | yes | str |
| position | BLOB |  | yes | bytes (108 bytes, starts 78daab56) |
| title | TEXT |  |  | str |
| url | TEXT |  | yes | str |
| is_showing_reader | BOOLEAN |  |  | int |
| is_pinned | BOOLEAN |  |  | int |
| reader_scroll_position_page_index | INTEGER |  |  | int |
| scene_id | TEXT |  |  | str |
| last_viewed_time | REAL |  |  | float (looks like Mac absolute time: s since 2001) |

### `metadata` (2 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| key | TEXT |  | yes | str |
| value |  |  |  | int |

## Timestamp columns (interpreted as Mac absolute time)

| column | non-null | min (UTC) | max (UTC) |
|---|---|---|---|
| `cloud_tab_devices.last_modified` | 3 | 2026-10-23T16:00:00+00:00 | 2026-10-24T16:00:00+00:00 |
| `cloud_tabs.last_viewed_time` | 485 | 2025-12-09T01:45:25+00:00 | 2026-10-25T05:00:12+00:00 |

## SafariTabs.db (local tabs & tab groups — possible later source)

- Path: `~/Library/Containers/com.apple.Safari/Data/Library/Safari/SafariTabs.db`

### `bookmark_title_words` (45 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| bookmark_id | INTEGER |  | yes | int |
| word | TEXT |  |  | str |
| word_index | INTEGER |  |  | int |

### `bookmarks` (16 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| special_id | INTEGER |  |  | int |
| parent | INTEGER |  |  | NULL |
| type | INTEGER |  |  | int |
| title | TEXT |  |  | str |
| url | TEXT |  |  | NULL |
| num_children | INTEGER |  |  | int |
| editable | INTEGER |  |  | int |
| deletable | INTEGER |  |  | int |
| hidden | INTEGER |  |  | int |
| hidden_ancestor_count | INTEGER |  |  | int |
| order_index | INTEGER |  | yes | int |
| external_uuid | TEXT |  |  | str |
| read | INTEGER |  |  | NULL |
| last_modified | REAL |  |  | NULL |
| server_id | TEXT |  |  | NULL |
| sync_key | TEXT |  |  | NULL |
| sync_data | BLOB |  |  | NULL |
| added | INTEGER |  |  | int |
| deleted | INTEGER |  |  | int |
| extra_attributes | BLOB |  |  | NULL |
| local_attributes | BLOB |  |  | NULL |
| fetched_icon | BOOL |  |  | int |
| icon | BLOB |  |  | NULL |
| dav_generation | INTEGER |  |  | int |
| locally_added | BOOL |  |  | int |
| archive_status | INTEGER |  |  | int |
| syncable | BOOL |  |  | int |
| web_filter_status | INTEGER |  |  | int |
| modified_attributes | UNSIGNED BIG INT |  |  | int |
| date_closed | REAL |  |  | NULL |
| last_selected_child | INTEGER |  |  | NULL |
| subtype | INTEGER |  |  | int |
| cookies_uuid | TEXT |  |  | NULL |
| local_storage_uuid | TEXT |  |  | NULL |
| session_storage_uuid | TEXT |  |  | NULL |

### `database_properties` (0 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| key | TEXT |  | yes | — |
| value | BLOB |  |  | — |

### `folder_ancestors` (4 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| folder_id | INTEGER |  | yes | int |
| ancestor_id | INTEGER |  | yes | int |

### `generations` (1 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| generation | INTEGER |  | yes | int |

### `participant_presence` (0 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | — |
| participant_id | TEXT |  |  | — |
| tab_group_server_id | TEXT |  |  | — |
| tab_server_id | TEXT |  |  | — |

### `session_state` (5 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| uuid | TEXT | 1 |  | str |
| data | BLOB |  | yes | bytes (801 bytes, starts 01000000) |

### `settings` (1 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| key | TEXT |  | yes | str |
| value | NUMERIC |  | yes | bytes (390 bytes, starts 62706c69) |
| generation | INTEGER |  | yes | int |
| device_identifier | TEXT |  | yes | str |
| parent | INTEGER |  |  | int |
| sync_data | BLOB |  |  | bytes (1908 bytes, starts 62706c69) |
| modified | INTEGER |  | yes | int |
| deleted | INTEGER |  | yes | int |
| server_id | TEXT |  |  | str |

### `sqlite_sequence` (3 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| name |  |  |  | str |
| seq |  |  |  | int |

### `sync_properties` (16 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| key | TEXT |  | yes | str |
| value | TEXT |  | yes | str |

### `windows` (1 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| active_tab_group_id | INTEGER |  |  | int |
| active_profile_id | INTEGER |  |  | int |
| date_closed | REAL |  |  | NULL |
| extra_attributes | BLOB |  |  | bytes (769 bytes, starts 62706c69) |
| is_last_session | INTEGER |  |  | int |
| local_tab_group_id | INTEGER |  |  | int |
| private_tab_group_id | INTEGER |  |  | int |
| scene_id | TEXT |  |  | NULL |
| uuid | TEXT |  | yes | str |
| restoration_archive | BLOB |  |  | bytes (209153 bytes, starts 62706c69) |

### `windows_profiles` (0 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | — |
| active_tab_group_id | INTEGER |  |  | — |
| profile_id | INTEGER |  | yes | — |
| window_id | INTEGER |  | yes | — |

### `windows_tab_groups` (1 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | int |
| active_tab_id | INTEGER |  |  | int |
| tab_group_id | INTEGER |  | yes | int |
| window_id | INTEGER |  | yes | int |

### `windows_unnamed_tab_groups` (0 rows)

| column | type | pk | not null | example kind |
|---|---|---|---|---|
| id | INTEGER | 1 |  | — |
| tab_group_id | INTEGER |  | yes | — |
| window_id | INTEGER |  | yes | — |

## Open questions

- Meaning of any timestamp columns above (verify against a tab opened at a known time).
