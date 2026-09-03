# Terraform

Roadmap step 14: "Use Terraform where appropriate for infrastructure."

## Scope

No cloud resources exist in this project (deliberately - see the original
plan's "don't buy additional hardware" constraint), so the usual
AWS/GCP/Azure Terraform story doesn't apply here. The genuinely valuable,
real-world-applicable fit instead: **GitHub itself as infrastructure** -
repo settings, branch protection, topics - managed declaratively via the
`integrations/github` provider rather than clicked through the web UI (which
is how the repo's settings were configured up to this point). Same category
of skill professionally (provider auth, state, plan/apply, import) as
managing cloud resources, just a different API.

Deliberately **not** used for Kubernetes or the Pi's host config - Argo CD
and Ansible already own those respectively (see docs/argocd.md,
docs/ansible.md). A third tool managing the same resources would just
create a new version of the "which system actually owns this state"
problem this project has been careful to avoid at each layer.

## Setup

- Auth via `GITHUB_TOKEN`/`GITHUB_OWNER` env vars, reusing the same `gh`
  CLI token (`gh auth token`) already used everywhere else in this
  project - no separate credential to create or rotate.
- State is **local** (`terraform.tfstate`, gitignored) - a real
  team/production setup would use a remote backend (Terraform Cloud, an
  S3-compatible bucket) so state isn't tied to one machine, but that's
  disproportionate complexity/cost for a solo project. Disclosed
  limitation, not an oversight.
- The repo **already existed** (created by hand with `gh repo create` back
  in Milestone 1) - so this started with `terraform import
  github_repository.homelab homelab`, not a fresh create. Import is
  arguably the more realistic real-world skill anyway: infrastructure
  that predates the Terraform config being written for it is the common
  case, not the exception.

## Log

- 2026-09-03: Wrote `repository.tf` matching the repo's actual existing
  settings (description, visibility, merge options, etc.) closely enough
  that `terraform plan` after the import showed **only** the two
  deliberate new changes (`topics`, `delete_branch_on_merge: false ->
  true`) plus 37 unchanged attributes - confirming the import was clean,
  not verified by assumption.

  Also wrote `github_branch_protection` for `main` (require the CI `test`
  status check before a PR merges) - deliberately mild, `enforce_admins =
  false` and no push restrictions, so it wouldn't break the direct-push
  workflow this whole project uses, including the CI deploy job that
  commits straight to `main` (see docs/cicd.md). Applying it failed with a
  real, legitimate GitHub constraint: **branch protection requires GitHub
  Pro or a public repository** - not available on a private repo's free
  tier. Confirmed with the user rather than assumed a fix (make it public
  for the free feature, or accept not having it) - kept the repo private,
  removed the branch protection resource, documented the constraint here
  instead of quietly working around it.

  `github_repository`'s two changes applied and verified independently via
  the GitHub API (not just `terraform apply` reporting success) - topics
  and `delete_branch_on_merge` both confirmed live on the actual repo.
