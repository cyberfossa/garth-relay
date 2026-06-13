variable "project_id" {
  description = "The GCP Project ID where resources will be created."
  type        = string
}

variable "region" {
  description = "The GCP region to deploy resources (e.g. Cloud Run, Artifact Registry)."
  type        = string
  default     = "europe-west3" # Frankfurt is a good default for Europe (+02:00)
}

variable "github_repository" {
  description = "The GitHub repository in owner/repo format (e.g., 'cyberfossa/garth-relay'). Used to configure Workload Identity Federation."
  type        = string
}

variable "app_name" {
  description = "The name of the application. Used as a prefix for GCP resources."
  type        = string
  default     = "garth-relay"
}
