resource "aws_ecr_repository" "app" {
  name                 = "${var.project_name}-app"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "ecr_repository_url" {
  description = "Push here first: docker tag/push an image with tag = var.lambda_image_tag to this URL before applying lambda.tf (its data source requires the image to already exist)."
  value       = aws_ecr_repository.app.repository_url
}
