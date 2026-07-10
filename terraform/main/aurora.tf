# Aurora Serverless v2 (Postgres) — the app's OWN database (approvals, flags,
# notes). Separate from AWP-SQL-PROD/SG360-TECH-PRD1, which are read-only
# on-prem sources accessed directly, not mirrored here.
#
# NOTE: this cluster needs a VPC + subnet group to exist in. If Lambda ends up
# reusing an existing VPC (per the Arkadiusz VPC-reuse question), point
# `db_subnet_group_name`/`vpc_security_group_ids` below at that same VPC so
# Lambda can actually reach it — a cluster in an unrelated VPC is unreachable
# from Lambda regardless of permissions. Left as variables, not hardcoded,
# since the actual VPC/subnet IDs aren't confirmed yet.

variable "vpc_id" {
  description = "VPC to deploy Aurora into — must be reachable from the Lambda's VPC config. Leave unset until confirmed with Arkadiusz."
  type        = string
  default     = ""
}

variable "aurora_subnet_ids" {
  description = "Subnet IDs for the Aurora DB subnet group — should match or be reachable from the Lambda's subnets."
  type        = list(string)
  default     = []
}

resource "aws_db_subnet_group" "aurora" {
  count      = var.vpc_id != "" ? 1 : 0
  name       = "${var.project_name}-aurora-subnet-group"
  subnet_ids = var.aurora_subnet_ids
}

resource "aws_security_group" "aurora" {
  count       = var.vpc_id != "" ? 1 : 0
  name        = "${var.project_name}-aurora-sg"
  description = "Allow Postgres from the Lambda security group only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Postgres from the Lambda dedicated security group only"
    from_port        = 5432
    to_port          = 5432
    protocol         = "tcp"
    security_groups  = [aws_security_group.lambda_sql_access[0].id]
  }
}

resource "aws_rds_cluster" "app" {
  count              = var.vpc_id != "" ? 1 : 0
  cluster_identifier = "${var.project_name}-aurora"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = "16.13" # matches the version already in use elsewhere in this account (pod-reporting-dev2-aurora)
  database_name      = "sg360_bol"
  master_username    = "sg360_admin"
  manage_master_user_password = true # stores the generated password in Secrets Manager automatically — no plaintext password in this config

  db_subnet_group_name   = aws_db_subnet_group.aurora[0].name
  vpc_security_group_ids = [aws_security_group.aurora[0].id]

  serverlessv2_scaling_configuration {
    min_capacity = 0   # scales to zero when idle — this app sees very light, bursty traffic
    max_capacity = 2.0
  }

  skip_final_snapshot = true # dev environment — acceptable; revisit before any production use
}

resource "aws_rds_cluster_instance" "app" {
  count              = var.vpc_id != "" ? 1 : 0
  cluster_identifier = aws_rds_cluster.app[0].id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.app[0].engine
  engine_version     = aws_rds_cluster.app[0].engine_version
}

output "aurora_endpoint" {
  value = var.vpc_id != "" ? aws_rds_cluster.app[0].endpoint : "not yet deployed — set var.vpc_id and var.aurora_subnet_ids first"
}

output "aurora_master_user_secret_arn" {
  description = "Secrets Manager ARN holding the auto-generated master password (manage_master_user_password = true)"
  value       = var.vpc_id != "" ? aws_rds_cluster.app[0].master_user_secret[0].secret_arn : "not yet deployed"
}
