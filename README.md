<p align="center">
  <img src="src/static/logo.png" width="180" alt="Garth Relay Logo">
</p>

<h1 align="center">Garth Relay</h1>

<p align="center">
  <strong>A lightweight, self-hosted FastAPI service to relay health metrics (such as weight and blood pressure) to Garmin Connect.</strong>
  <br>
  <i>Leverages the power of <a href="https://github.com/cyberfossa/garth-ng">garth</a> to securely synchronize fitness and health records.</i>
</p>

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

---

## Deployment to Google Cloud Platform (GCP)

This repository includes a Terraform configuration to provision all required GCP resources and a GitHub Actions workflow for automated deployments.

### Prerequisites

1. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) and [Terraform](https://developer.hashicorp.com/terraform/downloads).
2. Create a new GCP project and enable billing.
3. Authenticate with your GCP account:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```

### 1. Provision Infrastructure with Terraform

Navigate to the `infra` directory:
```bash
cd infra
```

Create a `terraform.tfvars` file (or pass them via command line) to define your variables:
```hcl
project_id        = "your-gcp-project-id"
github_repository = "your-github-username/your-forked-repo-name"
# region          = "europe-west3" # Optional, defaults to europe-west3
# app_name        = "garth-relay"  # Optional, defaults to garth-relay
# app_url         = ""             # Leave empty for initial bootstrap. Set to your service URL afterwards.
```

Initialize and apply the Terraform configuration:
```bash
terraform init
terraform apply
```

This will output the values needed for GitHub Actions configuration (like `wif_provider_name` and `github_actions_sa_email`), as well as create the Cloud Run service running a public dummy hello-world container. Note down the generated service URL.

### 2. Generate and Configure Secrets

Terraform creates placeholder secrets in Secret Manager. You must populate them with your actual values before deploying.

#### How to Generate Encryption & Session Keys:
Run these commands in your local terminal to generate secure random keys:
- **`APP_ENCRYPTION_KEY`** (32-byte AES key):
  ```bash
  python3 -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode())"
  ```
- **`APP_JWT_SECRET_KEY`** and **`APP_CSRF_SECRET`**:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

#### How to Create Google OAuth 2.0 Credentials:
1. In the Google Cloud Console, navigate to **APIs & Services** > **Credentials**.
2. Click **Create Credentials** at the top, and select **OAuth client ID**.
3. Select **Web application** as the application type.
4. Name your client (e.g., `Garth Relay Client`).
5. Under **Authorized redirect URIs**, add your redirect URLs:
   - For local development (optional): `http://localhost:8080/auth/callback` and `http://localhost:8080/connections/google/callback`
   - For Cloud Run: `https://<YOUR-CLOUD-RUN-URL>/auth/callback` and `https://<YOUR-CLOUD-RUN-URL>/connections/google/callback` (using the URL noted down in step 1).
6. Click **Create**. Copy the generated **Client ID** and **Client Secret**.

#### Update Terraform with your app_url:
Once you have the Cloud Run service URL:
1. Update `infra/terraform.tfvars` and set the `app_url` variable:
   ```hcl
   app_url = "https://<YOUR-CLOUD-RUN-URL>"
   ```
2. Run `terraform apply` again to propagate this URL to the container environment variables as `APP_GOOGLE_OAUTH_REDIRECT_URI`.

#### How to Populate Secret Manager:
1. Open **Secret Manager** in the GCP Console.
2. For each secret created by Terraform, click its name, click **New Version**, paste the corresponding value, and click **Add Version**:
   - `${app_name}-APP_ENCRYPTION_KEY`: The generated 32-byte AES key.
   - `${app_name}-APP_JWT_SECRET_KEY`: The generated JWT signing key.
   - `${app_name}-APP_CSRF_SECRET`: The generated CSRF protection key.
   - `${app_name}-APP_GOOGLE_CLIENT_ID`: Your Google OAuth Client ID.
   - `${app_name}-APP_GOOGLE_CLIENT_SECRET`: Your Google OAuth Client Secret.
   - `${app_name}-APP_GOOGLE_HEALTH_WEBHOOK_SECRET`: A secure random token used to authorize the webhook handshake challenge. You can generate a random token using `openssl rand -hex 32` or similar.

### 3. Configure OAuth Consent Screen (Manual Step)

Before your application can authenticate users and access their Google Health data, you must manually configure the OAuth Consent Screen in the Google Cloud Console (this step cannot be automated via Terraform):

1. In the Google Cloud Console, navigate to **APIs & Services** > **OAuth consent screen**.
2. Select **External** as the user type (or **Internal** if using Google Workspace).
3. Fill in the required app details (App name, support email, developer contact email).
4. Click **Save and Continue**.
5. In the **Scopes** step, click **Add or Remove Scopes** and add:
   - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` (This is the scope required to sync Google Health metrics).
6. In the **Test users** step, add your Gmail account email address. (Required while the app is in "Testing" status).
7. Save.

### 4. Configure GitHub Actions Secrets & Variables

In your forked GitHub repository, navigate to **Settings** -> **Secrets and variables** -> **Actions** and add the following:

#### Secrets
- `GCP_PROJECT_ID`: Your GCP Project ID.
- `GCP_WIF_PROVIDER`: The `wif_provider_name` output from Terraform.
- `GCP_SERVICE_ACCOUNT`: The `github_actions_sa_email` output from Terraform.

#### Variables (Optional)
- `GCP_REGION`: The GCP region used in Terraform (defaults to `europe-west3`).
- `GCP_APP_NAME`: The app name used in Terraform (defaults to `garth-relay`).

### 5. Deploy

Push any change to the `main` branch, or trigger the workflow manually under the **Actions** tab. The GitHub Actions runner will authenticate using Workload Identity Federation (WIF), build the Docker image, push it to Artifact Registry, and update the Cloud Run service container image.


### 6. Register Google Health Webhook Subscriber

Once the application is successfully deployed and running on Cloud Run, you must register the webhook endpoint with the Google Health API. This is a one-time setup step:

1. Authenticate with your GCP account:
   ```bash
   gcloud auth application-default login
   ```
2. Run the helper registration script, substituting your project number, Cloud Run URL, and webhook secret:
   ```bash
   uv run scripts/register_subscriber.py \
     --project-number="<YOUR-GCP-PROJECT-NUMBER>" \
     --webhook-url="https://<YOUR-CLOUD-RUN-URL>/webhooks/google-health" \
     --webhook-secret="<YOUR-WEBHOOK-SECRET>"
   ```

To delete/unregister the webhook subscriber, you can run the script with the `--delete` flag (which only requires the `--project-number`):
   ```bash
   uv run scripts/register_subscriber.py \
     --project-number="<YOUR-GCP-PROJECT-NUMBER>" \
     --delete
   ```

To check the current status of the subscriber and retrieve a list of all active user subscriptions, run the script with the `--status` flag:
   ```bash
   uv run scripts/register_subscriber.py \
     --project-number="<YOUR-GCP-PROJECT-NUMBER>" \
     --status
   ```

*(Note: The project number is a 12-digit number found on your GCP Console Dashboard, not the text-based Project ID. The webhook secret must match the value you added to GCP Secret Manager for `APP_GOOGLE_HEALTH_WEBHOOK_SECRET`).*
