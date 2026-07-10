variable "aws_region" {
  description = "AWS region for the Terraform state backend resources."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "Globally-unique S3 bucket name to store Terraform remote state."
  type        = string
  # No default — must be supplied. Recommended: sg360-bol-tfstate-610614956027
}
