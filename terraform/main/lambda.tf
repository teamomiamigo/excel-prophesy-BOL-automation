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

  # Required for aws_lambda_provisioned_concurrency_config below — provisioned
  # concurrency can only target a published, immutable version, never $LATEST.
  # Each apply that changes image_uri publishes a new numbered version; the
  # "live" alias and its provisioned concurrency move to it automatically.
  publish = true

  # 29s matches API Gateway's hard request timeout (see CLAUDE.md deployment
  # notes) — no point allowing Lambda to run longer than the caller will wait.
  timeout     = 29
  memory_size = 512

  vpc_config {
    subnet_ids         = ["subnet-0734a08d41d98120f", "subnet-0ca80749a96a812db"]
    security_group_ids = [aws_security_group.lambda_sql_access[0].id]
  }

  # NOTE (2026-07-23): config.py supports rebuilding DATABASE_URL fresh from
  # Aurora's own AWS-managed, auto-rotated master secret at every cold start
  # (RDS_MASTER_SECRET_ARN/DB_HOST/DB_PORT/DB_NAME) instead of the manually-
  # synced copy below — the real fix for the credential-drift outage this is.
  # NOT wired up here: iam.tf's matching secretsmanager:GetSecretValue grant
  # on the RDS secret exists in config but fails to apply — iam:PutRolePolicy
  # denied for the deploying user (confirmed again 2026-07-23, same as
  # 2026-07-16). Setting these 4 env vars without that grant applied makes
  # every cold start fail on AccessDenied instead of stale-password — worse,
  # not better. Only re-add them once an account admin has granted that
  # permission and `terraform apply` on iam.tf's lambda_secrets_access
  # resource has actually succeeded.
  environment {
    variables = {
      AWS_SECRET_NAME = "sg360-bol-live-credentials"
    }
  }
}

output "lambda_function_name" {
  value = aws_lambda_function.app.function_name
}

# --- Provisioned concurrency (added 2026-07-20) ---
# A cold Lambda execution environment (fresh container, Python imports, Secrets
# Manager fetch, first Aurora connection) measured at ~13-23s in live testing —
# on top of the live AWP-SQL-PROD query itself (~15-23s), this reliably pushed
# POST /api/admin/pull past API Gateway's hard 30s integration timeout. Keeping
# one execution environment permanently warm removes the cold-start cost
# entirely (does NOT reduce the on-prem query's own network latency — that part
# is unrelated to Lambda and still varies run to run).
resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.app.function_name
  function_version = aws_lambda_function.app.version
}

resource "aws_lambda_provisioned_concurrency_config" "app" {
  function_name                     = aws_lambda_function.app.function_name
  qualifier                         = aws_lambda_alias.live.name
  provisioned_concurrent_executions = 1
}
