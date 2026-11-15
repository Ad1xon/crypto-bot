variable "owner" {
  description = "GitHub user/org owning the repository."
  type        = string
  default     = "Ad1xon"
}

variable "repository" {
  description = "Repository name."
  type        = string
  default     = "crypto-bot"
}

variable "required_checks" {
  description = "CI job names (as shown in the PR checks list) that must pass before merging into main."
  type        = list(string)
  default = [
    "Lint & type check",
    "Tests (py3.11)",
    "Tests (py3.12)",
    "Validate compose & monitoring config",
    "Terraform fmt & validate (infra/terraform)",
    "Terraform fmt & validate (infra/github)",
    "Build, scan & push image",
  ]
}
