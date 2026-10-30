terraform {
  required_version = ">= 1.6.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.49"
    }
  }

  # Remote state (recommended once more than one person/machine applies).
  # Hetzner Object Storage is S3-compatible:
  #
  # backend "s3" {
  #   bucket                      = "crypto-quant-tfstate"
  #   key                         = "prod/terraform.tfstate"
  #   region                      = "eu-central"
  #   endpoints                   = { s3 = "https://fsn1.your-objectstorage.com" }
  #   skip_credentials_validation = true
  #   skip_region_validation      = true
  #   skip_requesting_account_id  = true
  #   skip_metadata_api_check     = true
  #   use_path_style              = true
  # }
}

provider "hcloud" {
  token = var.hcloud_token
}
