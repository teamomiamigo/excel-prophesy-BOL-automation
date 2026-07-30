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

  # (2026-07-30): confirmed live via `aws iam get-role-policy` that
  # iam.tf's lambda_secrets_access grant already includes the RDS-managed
  # master secret ARN — the iam:PutRolePolicy block described below (and in
  # CLAUDE.md, as of 2026-07-23) is stale; the grant is in effect now. Wiring
  # up RDS_MASTER_SECRET_ARN/DB_HOST/DB_PORT/DB_NAME so config.py rebuilds
  # DATABASE_URL fresh from Aurora's own auto-rotated secret at every cold
  # start, eliminating the manually-synced copy in sg360-bol-live-credentials
  # (and the credential-drift class of outage it caused, recurring 2026-07-16
  # and 2026-07-30).
  environment {
    variables = {
      AWS_SECRET_NAME       = "sg360-bol-live-credentials"
      RDS_MASTER_SECRET_ARN = aws_rds_cluster.app[0].master_user_secret[0].secret_arn
      DB_HOST               = aws_rds_cluster.app[0].endpoint
      DB_PORT               = tostring(aws_rds_cluster.app[0].port)
      DB_NAME               = aws_rds_cluster.app[0].database_name
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
