# Imported, not created - see docs/terraform.md. Values below match what
# was already configured by hand via `gh repo create` back in Milestone 1,
# so the import produces a clean (empty) diff on everything except the
# two settings this pass deliberately changes: topics and
# delete_branch_on_merge.
resource "github_repository" "homelab" {
  name        = "homelab"
  description = "Self-hosted cloud/AI platform on a Raspberry Pi 5 + GPU desktop: Docker/K3s, FastAPI, PostgreSQL/pgvector, local LLM RAG, CI/CD, observability"
  visibility  = "private"

  has_issues   = true
  has_projects = true
  has_wiki     = false

  allow_merge_commit = true
  allow_squash_merge = true
  allow_rebase_merge = true

  # Actual, new changes this pass makes - not just re-declaring existing
  # state:
  delete_branch_on_merge = true # was false; stale branches were piling up manually
  topics = [
    "homelab", "kubernetes", "k3s", "raspberry-pi", "fastapi",
    "argocd", "terraform", "ansible", "self-hosted", "rag"
  ]
}
