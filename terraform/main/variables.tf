variable "aws_region" {
  description = "AWS region for all Stage 2 resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used as a prefix for all Stage 2 resource names."
  type        = string
  default     = "sg360-bol"
}

variable "lambda_image_tag" {
  description = "Tag of the image already pushed to ECR to deploy. Push an image with this tag (see ecr.tf output) before applying lambda.tf/apigateway.tf — the aws_ecr_image data source requires it to already exist."
  type        = string
  default     = "stage1"
}
