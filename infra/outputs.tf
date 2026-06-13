output "gcp_project_id" {
  description = "The GCP Project ID."
  value       = var.project_id
}

output "gcp_region" {
  description = "The GCP Region."
  value       = var.region
}

output "wif_provider_name" {
  description = "The Workload Identity Provider resource name. Use this value for GCP_WIF_PROVIDER in GitHub Secrets."
  value       = google_iam_workload_identity_pool_provider.github_provider.name
}

output "github_actions_sa_email" {
  description = "The email of the GitHub Actions deployer Service Account. Use this value for GCP_SERVICE_ACCOUNT in GitHub Secrets."
  value       = google_service_account.github_actions_sa.email
}

output "artifact_registry_repo_uri" {
  description = "The URI of the Artifact Registry repository."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.app_name}"
}

output "cloud_run_sa_email" {
  description = "The email of the Cloud Run runner Service Account."
  value       = google_service_account.cloud_run_sa.email
}
