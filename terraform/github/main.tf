terraform {
  required_version = ">= 1.9"
  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }
}

# Auth via the GITHUB_TOKEN env var (reuses the same `gh` CLI token this
# whole project already authenticates with - no separate credential to
# manage). Owner likewise via GITHUB_OWNER, not hardcoded here.
provider "github" {}
