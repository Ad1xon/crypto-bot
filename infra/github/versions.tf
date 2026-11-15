terraform {
  required_version = ">= 1.6.0"

  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }

  # Local state (gitignored), same as infra/terraform. Kept as a separate root so that
  # Hetzner plans don't need a GitHub token and vice versa.
}

# Auth: export GITHUB_TOKEN=<fine-grained PAT> (repo: crypto-bot,
# permissions: Administration = Read & write, Metadata = Read).
# If GITHUB_TOKEN is unset, the provider falls back to `gh auth token`.
provider "github" {
  owner = var.owner
}
