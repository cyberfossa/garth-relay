terraform {
  required_version = ">= 1.3.0"
  backend "gcs" {
    bucket = "garth-relay-tfstate"
    prefix = "terraform/state"
  }
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- 1. Enable Required GCP APIs ---
locals {
  services = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "artifactregistry.googleapis.com",
    "health.googleapis.com"
  ]
}

resource "google_project_service" "enabled_apis" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --- 2. Artifact Registry for Docker Images ---
resource "google_artifact_registry_repository" "repo" {
  depends_on    = [google_project_service.enabled_apis["artifactregistry.googleapis.com"]]
  location      = var.region
  repository_id = var.app_name
  description   = "Docker repository for ${var.app_name} images"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }

  cleanup_policy_dry_run = false
}

# --- 3. Cloud Firestore Database ---
resource "google_firestore_database" "database" {
  depends_on       = [google_project_service.enabled_apis["firestore.googleapis.com"]]
  project          = var.project_id
  name             = "(default)"
  location_id      = var.region
  type             = "FIRESTORE_NATIVE"
  concurrency_mode = "OPTIMISTIC"

  # Prevent accidental deletion of the database
  lifecycle {
    prevent_destroy = false
  }
}

# Enable TTL for OAuth states in Firestore
resource "google_firestore_field" "oauth_states_ttl" {
  project    = var.project_id
  database   = google_firestore_database.database.name
  collection = "oauth_states"
  field      = "expire_at"

  ttl_config {}
}

# Composite index for querying the latest successful sync log
resource "google_firestore_index" "sync_logs_index" {
  project    = var.project_id
  database   = google_firestore_database.database.name
  collection = "sync_logs"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }

  fields {
    field_path = "timestamp"
    order      = "DESCENDING"
  }
}

# --- 4. Secret Manager Placeholders ---
locals {
  secret_names = [
    "APP_ENCRYPTION_KEY",
    "APP_JWT_SECRET_KEY",
    "APP_CSRF_SECRET",
    "APP_GOOGLE_CLIENT_ID",
    "APP_GOOGLE_CLIENT_SECRET",
    "APP_GOOGLE_HEALTH_WEBHOOK_SECRET"
  ]
}

resource "google_secret_manager_secret" "secrets" {
  for_each   = toset(local.secret_names)
  depends_on = [google_project_service.enabled_apis["secretmanager.googleapis.com"]]
  secret_id  = "${var.app_name}-${each.value}"

  replication {
    auto {}
  }
}

# --- 5. IAM & Service Account for running Cloud Run ---
resource "google_service_account" "cloud_run_sa" {
  depends_on   = [google_project_service.enabled_apis["iam.googleapis.com"]]
  account_id   = "${var.app_name}-runner"
  display_name = "Service Account for running ${var.app_name} on Cloud Run"
}

# Grant Firestore access to the Cloud Run service account
resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# Grant Secret Manager access to the Cloud Run service account
resource "google_secret_manager_secret_iam_member" "secret_access" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# --- 6. Workload Identity Federation (WIF) for GitHub Actions ---
resource "google_iam_workload_identity_pool" "github_pool" {
  depends_on                = [google_project_service.enabled_apis["iam.googleapis.com"]]
  workload_identity_pool_id = "${var.app_name}-github-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Identity pool for GitHub Actions deployment"
}

resource "google_iam_workload_identity_pool_provider" "github_provider" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_pool.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub Provider"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Dedicated Service Account for GitHub Actions CI/CD execution
resource "google_service_account" "github_actions_sa" {
  depends_on   = [google_project_service.enabled_apis["iam.googleapis.com"]]
  account_id   = "${var.app_name}-github-deployer"
  display_name = "Service Account for GitHub Actions deployment"
}

# Allow GitHub Actions provider to impersonate the deployment Service Account
# ONLY when triggered from the specified repository (var.github_repository)
resource "google_service_account_iam_member" "github_actions_impersonation" {
  service_account_id = google_service_account.github_actions_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_pool.name}/attribute.repository/${var.github_repository}"
}

# Permissions for the deployment Service Account:
# A. Write to Artifact Registry (upload Docker image)
resource "google_artifact_registry_repository_iam_member" "github_actions_registry_writer" {
  location   = google_artifact_registry_repository.repo.location
  repository = google_artifact_registry_repository.repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_actions_sa.email}"
}

# B. Manage Cloud Run Service
resource "google_project_iam_member" "github_actions_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.github_actions_sa.email}"
}

# C. Act as the Cloud Run Runner Service Account (to assign it to the Cloud Run service)
resource "google_service_account_iam_member" "github_actions_act_as_runner" {
  service_account_id = google_service_account.cloud_run_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_actions_sa.email}"
}

# --- 7. Cloud Run Service ---
resource "google_cloud_run_v2_service" "app" {
  name     = var.app_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run_sa.email

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello" # bootstrap image, overridden by CI

      ports {
        container_port = 8080
      }

      env {
        name  = "APP_GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "APP_GOOGLE_OAUTH_REDIRECT_URI"
        value = var.app_url != "" ? "${var.app_url}/auth/callback" : ""
      }

      # Secrets mapped as environment variables from Secret Manager
      dynamic "env" {
        for_each = toset(local.secret_names)
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }

  # Ensure APIs, database, secrets and permissions are ready first
  depends_on = [
    google_project_service.enabled_apis,
    google_firestore_database.database,
    google_secret_manager_secret.secrets,
    google_secret_manager_secret_iam_member.secret_access,
  ]
}

# Allow public unauthenticated access to the Cloud Run service
resource "google_cloud_run_v2_service_iam_member" "public_access" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

