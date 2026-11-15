output "ruleset_id" {
  value = github_repository_ruleset.main.ruleset_id
}

output "repository_url" {
  value = github_repository.this.html_url
}
