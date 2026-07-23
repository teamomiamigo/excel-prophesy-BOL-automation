data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Grants CloudWatch Logs write access — the minimum a Lambda function needs to run.
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Required to attach the Lambda to a VPC (ec2:CreateNetworkInterface etc.) —
# needed for live-mode access to AWP-SQL-PROD/SG360-TECH-PRD1. Attached ahead
# of the actual vpc_config block being added, since it's harmless on its own.
resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Scoped to exactly the two secrets the Lambda needs — not a blanket
# secretsmanager:* grant across the account. The RDS-managed secret lets
# config.py rebuild DATABASE_URL fresh from Aurora's own auto-rotated
# password at every cold start, instead of a manually-synced copy going
# stale on rotation (see lambda.tf's RDS_MASTER_SECRET_ARN env var).
data "aws_iam_policy_document" "lambda_secrets_access" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:us-east-1:610614956027:secret:sg360-bol-live-credentials-qR83EV",
      aws_rds_cluster.app[0].master_user_secret[0].secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "lambda_secrets_access" {
  name   = "${var.project_name}-lambda-secrets-access"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_secrets_access.json
}
