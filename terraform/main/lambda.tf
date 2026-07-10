# Requires an image already pushed to the ECR repo (see ecr.tf output) with
# tag = var.lambda_image_tag — this data source resolves at plan time and
# fails if the image doesn't exist yet. Apply ecr.tf's repository first,
# push the image, then apply this file.
data "aws_ecr_image" "app" {
  repository_name = aws_ecr_repository.app.name
  image_tag       = var.lambda_image_tag
}

resource "aws_lambda_function" "app" {
  function_name = "${var.project_name}-api"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}@${data.aws_ecr_image.app.image_digest}"

  # 29s matches API Gateway's hard request timeout (see CLAUDE.md deployment
  # notes) — no point allowing Lambda to run longer than the caller will wait.
  timeout     = 29
  memory_size = 512

  vpc_config {
    subnet_ids         = ["subnet-0734a08d41d98120f", "subnet-0ca80749a96a812db"]
    security_group_ids = [aws_security_group.lambda_sql_access[0].id]
  }

  environment {
    variables = {
      AWS_SECRET_NAME = "sg360-bol-live-credentials"
    }
  }
}

output "lambda_function_name" {
  value = aws_lambda_function.app.function_name
}
