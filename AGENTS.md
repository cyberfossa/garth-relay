# AI Agent Guidelines (agents.md)

This file provides context, rules, and guidelines for AI coding agents (such as Claude Code, Gemini, or Cursor) working on the `garth-relay` codebase.

---

## 🛠️ Environment & Package Management (`uv`)

This project uses **`uv`** as its package manager and environment manager.
- **Do not** use `pip`, `pipenv`, or standard `venv` directly.
- **Do not** manually run `python -m venv .venv`.

### Core `uv` Commands

| Task | Command |
|------|---------|
| Initialize/Sync environment | `uv sync --all-extras` |
| Add a dependency | `uv add <package>` |
| Add a dev dependency | `uv add --dev <package>` |
| Run a command in venv | `uv run <command>` |
| Run tests | `uv run pytest` |
| Run development server | `uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload` |

---

## 🏗️ Project Architecture

`garth-relay` is a FastAPI application designed to relay health metrics (such as weight) to Garmin Connect.

### File Structure

- **`src/`** - Main application source code
  - **`main.py`** - FastAPI app creation, middleware configuration, and route registration.
  - **`config.py`** - Settings and environment variable validation using `environ-config`.
  - **`auth/`** - Google/Garmin session and login helpers.
  - **`db/`** - Firestore client initialization and helper classes.
  - **`routes/`** - Router files handling request endpoints:
    - `auth.py`: Authentication endpoints (JWT token verification, user loading).
    - `connections.py`: Google/Garmin OAuth flows and connections.
    - `pages.py`: SSR page rendering (Dashboard, home page, login UI).
    - `polling.py`: Cloud Scheduler endpoint for periodically triggering sync.
    - `sync_weight.py`: Weight sync views.
  - **`services/`** - Business logic for fetching and processing health metrics:
    - `google_health_client.py`: Interacts with Google Health / Fitbit API.
    - `garmin_client.py`: Wrapper around `garth-ng` client.
    - `sync_orchestrator.py`: Runs the weight and composition sync process.
  - **`templates/`** - Jinja2 UI templates using HTML and HTMX.
- **`tests/`** - Unit and integration tests (using `pytest` and `pytest-asyncio`).
- **`infra/`** - Terraform infrastructure files for GCP provisioning.

---

## 🧪 Testing and Quality Control

### Running Tests
Make sure all dependencies are synced first:
```bash
uv sync --all-extras
```

Run the pytest suite:
```bash
uv run pytest
```

### Linting & Formatting
We use Ruff for linting and formatting. Run these commands to verify code style:
```bash
uv run ruff check
uv run ruff format
```

For static type checking:
```bash
uv run mypy src
```

---

## 💡 Guidelines for AI Agents

1. **Use `uv`**: Always prefix python execution commands with `uv run`. When installing new packages, use `uv add` or `uv add --dev`.
2. **Code Style**: Adhere to PEP 8 standards with a line-length limit of 120 (as defined in `pyproject.toml`).
3. **Extending Sync Sources**: If adding a new sync source, check [README.md](README.md#adding-a-new-sync-source) for the step-by-step factory registration pattern.

---

## 🌍 Infrastructure as Code (`terraform`)

The GCP infrastructure is managed using **Terraform** located in the `infra/` directory.

- **Do not** modify GCP resources manually in the Google Cloud Console to avoid state drift.
- Configuration variables should be defined in `infra/terraform.tfvars`.

### Core Terraform Commands

| Task | Command |
|------|---------|
| Initialize Terraform | `terraform init` |
| Preview changes | `terraform plan` |
| Apply changes | `terraform apply` |

For deployment details, check [README.md](README.md#deployment-to-google-cloud-platform-gcp).

