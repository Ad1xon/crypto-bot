# Repository settings + branch ruleset for main, so nothing has to be clicked in the GitHub UI.
# Needed by .github/workflows/dependabot-automerge.yml:
#   - allow_auto_merge  -> `gh pr merge --auto` is allowed at all
#   - ruleset           -> auto-merge waits until the required CI checks are green

# The repository already exists - adopt it into state instead of creating it.
import {
  to = github_repository.this
  id = var.repository
}

resource "github_repository" "this" {
  name       = var.repository
  visibility = "public"

  allow_auto_merge       = true
  delete_branch_on_merge = true # clean up dependabot/* branches after merge

  # Merge methods are set explicitly: the API read of these fields is unreliable, and if they
  # were only in ignore_changes the provider would still send the (wrong) state value on update.
  allow_squash_merge = true # used by dependabot-automerge.yml
  allow_merge_commit = true # used for manual PR merges so far (e.g. PR #13)
  allow_rebase_merge = true

  lifecycle {
    # Removing this resource from config must NEVER delete the real repository.
    prevent_destroy = true

    # Only the attributes above are managed here; everything else stays as configured in the UI
    # (otherwise provider defaults could e.g. silently disable Issues or the Wiki).
    ignore_changes = [
      description,
      homepage_url,
      topics,
      has_issues,
      has_discussions,
      has_projects,
      has_wiki,
      has_downloads,
      allow_update_branch,
      squash_merge_commit_title,
      squash_merge_commit_message,
      merge_commit_title,
      merge_commit_message,
      web_commit_signoff_required,
      security_and_analysis,
      pages,
      template,
      is_template,
      archived,
      auto_init,
    ]
  }
}

resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = github_repository.this.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  # Repository admins may bypass, so direct pushes to main keep working.
  # Dependabot / github-actions[bot] are not admins -> their auto-merge must wait for green CI.
  bypass_actors {
    actor_id    = 5 # built-in "Repository admin" role
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  rules {
    deletion         = true # main can't be deleted
    non_fast_forward = true # no force-push to main

    required_status_checks {
      strict_required_status_checks_policy = false

      dynamic "required_check" {
        for_each = var.required_checks
        content {
          context        = required_check.value
          integration_id = 15368 # GitHub Actions app - checks must come from Actions, not any status API caller
        }
      }
    }
  }
}
