# Which Terraform and which "provider" (plugin that talks to a cloud) we need.
# `terraform init` reads this and downloads the AWS provider.
terraform {
  required_version = ">= 1.5" # 1.5+ supports `import` blocks (imports.tf)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# Same account and region the AWS CLI uses (profile "smogscope" in ~/.aws/credentials).
provider "aws" {
  region  = var.region
  profile = "smogscope"
}
