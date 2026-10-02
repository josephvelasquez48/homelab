# main requires the PR gate (.github/workflows/pr-gate.yml) to pass. The
# gate rather than any app's `test` job: those are path-filtered, so a docs
# or Ansible PR runs none of them and would wait forever on a required
# check that never reports.
#
# A ruleset rather than classic branch protection for the bypass: CI's
# deploy jobs push "[skip ci]" tag bumps straight to main, which a
# required check would otherwise reject. They push with the DEPLOY_KEY
# deploy key, because on a personal repo a ruleset can only exempt roles
# and deploy keys - not the GitHub Actions app (that 422s on apply).
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

  # Applies to every deploy key, but only one can push: ci-deploy, the
  # DEPLOY_KEY secret used by the deploy jobs' checkout. The Pi's
  # pi5-homelab-deploy is read-only, so exempting it changes nothing.
  bypass_actors {
    actor_type  = "DeployKey"
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
