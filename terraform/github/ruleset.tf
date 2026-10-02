# main requires the PR gate (.github/workflows/pr-gate.yml) to pass. The
# gate rather than any app's `test` job: those are path-filtered, so a docs
# or Ansible PR runs none of them and would wait forever on a required
# check that never reports.
#
# A ruleset rather than classic branch protection for the bypass: CI's
# deploy jobs push "[skip ci]" tag bumps straight to main as
# github-actions[bot], which a required check would otherwise reject.
resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = github_repository.homelab.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  # The GitHub Actions app, i.e. the deploy jobs' GITHUB_TOKEN pushes.
  bypass_actors {
    actor_id    = 15368
    actor_type  = "Integration"
    bypass_mode = "always"
  }

  rules {
    required_status_checks {
      required_check {
        context = "gate"
        # Only GitHub Actions can report it, so nothing else can satisfy
        # the check by posting a status with the same name.
        integration_id = 15368
      }
    }
  }
}
