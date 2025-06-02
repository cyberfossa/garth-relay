# Garth Relay

Relay health metrics from external sources to Garmin Connect using [garth](https://github.com/cyberfossa/garth-ng).

---

## What It Does

Garth Relay bridges health data from various sources into Garmin Connect. It runs as a self-hosted FastAPI service (Cloud Run compatible, scale-to-zero) and supports multiple users with independent OAuth flows.

### Supported Sources

| Source | Status |
|--------|--------|
| Google Health / Fitbit scale (Aria 2) | 🔜 Planned |
| ESP32 MiScale Bluetooth webhook | 🔜 Planned |
| OMRON blood pressure monitor | 🔜 Planned |

### Features

- Weight and body composition sync to Garmin Connect
- Multi-user support with per-user encrypted credentials
- Cloud Scheduler polling (every 15 min)
- Manual sync via dashboard
- SSR dashboard with HTMX
- AES-256-GCM token encryption with per-user AAD

---

## Architecture

| Component | Role |
|-----------|------|
| **FastAPI** | Web app — auth, dashboard, polling endpoint |
| **garth-ng** | Garmin Connect client (body composition, session management) |
| **Firestore** | Per-user encrypted tokens, Garmin sessions, sync logs |
| **Cloud Run** | Hosting — scale-to-zero |
| **Cloud Scheduler** | Triggers polling endpoint periodically |

```text
External Source → Google Health API / Webhook
                        ↓
                Cloud Scheduler → Cloud Run (Garth Relay)
                                        ↓
                                Garmin Connect
```

---

## License

MIT

---

## Adding a New Sync Source

The sync pages follow an extensible pattern. Use these steps to add a new health metric source (e.g., blood pressure):

1. **File structure**: Create `src/routes/sync_X.py`, `src/templates/sync-X.html`, and `tests/test_sync_X.py`.
2. **Router factory**: Implement `create_sync_X_router(db, config, encryptor=None)` in your new route file.
3. **Shared utilities**: Use `src.routes.sync_common` for `compare_measurements_with_garmin` and `build_sync_table_html`.
4. **URL pattern**: Register routes under `/sync/{source}` (e.g., `/sync/blood-pressure`).
5. **Templates**: Extend `base.html` and include `partials/sync-nav.html` for navigation tabs.
6. **Nav tabs**: Update `src/templates/partials/sync-nav.html` to add a new `<li>` for the source.
7. **Main registration**: Register the new router in `src/main.py` using the factory pattern.
