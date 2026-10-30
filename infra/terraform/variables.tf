variable "hcloud_token" {
  description = "Hetzner Cloud API token (Read & Write). Pass via TF_VAR_hcloud_token, never commit."
  type        = string
  sensitive   = true
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "crypto-quant"
}

variable "environment" {
  description = "Deployment environment label."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "location" {
  description = "Hetzner location. fsn1/nbg1/hel1 (EU), ash/hil (US), sin (closest to Hyperliquid's Tokyo validators)."
  type        = string
  default     = "fsn1"
}

variable "server_type" {
  description = "Server type. FinBERT + torch need ~1.5 GB RAM; 4 GB leaves room for the monitoring stack."
  type        = string
  default     = "cx22"
}

variable "image" {
  description = "OS image."
  type        = string
  default     = "ubuntu-24.04"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key installed for the deploy user."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach SSH. Restrict to your own IP in production."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "data_volume_size_gb" {
  description = "Size of the persistent volume holding tick parquet files and GEX history."
  type        = number
  default     = 20

  validation {
    condition     = var.data_volume_size_gb >= 10
    error_message = "Hetzner volumes are at least 10 GB."
  }
}

variable "repo_url" {
  description = "Git repository cloned onto the server (holds docker-compose.yml and monitoring config)."
  type        = string
  default     = "https://github.com/Ad1xon/crypto-bot.git"
}

variable "repo_ref" {
  description = "Branch or tag checked out on first boot."
  type        = string
  default     = "main"
}

variable "enable_backups" {
  description = "Enable Hetzner daily server backups (+20% of server price)."
  type        = bool
  default     = false
}

variable "enable_protection" {
  description = "Protect server and volume against accidental delete/rebuild."
  type        = bool
  default     = true
}
