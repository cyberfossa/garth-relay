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
