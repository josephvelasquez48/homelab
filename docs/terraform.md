# Terraform

Roadmap step 14. There are no cloud resources in this project, so Terraform
manages the one piece of real external infrastructure: **the GitHub repo
itself** - its settings and topics - with the `integrations/github`
provider (`terraform/github/`). Same skills as cloud work: provider auth,
state, plan/apply, import.

It doesn't manage Kubernetes or the Pi; Argo CD and Ansible own those, and a
third tool on the same resources would blur who owns what.

## Setup

- **Auth:** `GITHUB_TOKEN` and `GITHUB_OWNER`, reusing the `gh` CLI's token
  (`gh auth token`) - no new credential.
- **State is local** (`terraform.tfstate`, gitignored). A team would use a
  remote backend; for one person it's disproportionate. It's included in
  operator backups.
- **Imported, not created.** The repo already existed, so it started with
  `terraform import github_repository.homelab homelab` - the realistic case,
  since infrastructure usually predates its Terraform.

## What happened

- **A clean import, checked by the plan:** after importing, `terraform plan`
  showed only the two intended changes - adding topics and turning on
  `delete_branch_on_merge` - with 37 attributes unchanged. After applying,
  both were confirmed through the GitHub API, not just Terraform's output.
- **Branch protection was dropped.** Applying it failed: on the free plan,
  branch protection needs a public repo, and the repo was private then. It
  was kept private and the resource removed.

## Out of date: visibility

**The repo is now public, but `repository.tf` still says
`visibility = "private"`.** A `terraform apply` would try to make it
private again. Update the file to `"public"` (and run `terraform plan` to
confirm no other drift) before applying anything. Being public also makes
the branch protection above possible now - though it must still let CI's
deploy jobs commit to `main` ([cicd.md](cicd.md)).
