# Gated behind an explicit variable (default false) so this can never be
# created by an incidental `terraform apply` in this directory before the
# CHG ticket is actually approved — flip to true only once approved.
variable "chg_approved_sql_access" {
  description = "Set to true only after the CHG ticket granting Lambda-to-prod-SQL network access is approved."
  type        = bool
  default     = false
}

# Matches exactly what was described in the approved CHG ticket — outbound
# SQL Server (1433) to these two specific hosts, nothing else. Do not widen
# this beyond what was represented to security/CAB.
#
# The SG360-TECH-PRD1 egress rule the CHG ticket approved was removed 2026-07-20:
# it only ever existed to support a direct SQL connection that turned out to be
# dead code (see documentation/Developmental Documentation.md's 2026-07-20 entry —
# ShipperPlus_Segerdahl never lived on that host). AWP-SQL-PROD egress remains,
# unchanged and still within what was approved.
#
# All rules for this security group are managed as separate
# aws_security_group_rule resources below, NOT as inline egress blocks here.
# Mixing the two causes Terraform to treat externally-added rules (like the
# Aurora egress rule, added from aurora.tf to avoid a dependency cycle) as
# drift and try to remove them on every apply. Keeping this resource rule-free
# avoids that conflict entirely.
resource "aws_security_group" "lambda_sql_access" {
  count       = var.chg_approved_sql_access ? 1 : 0
  name        = "${var.project_name}-lambda-sql-access"
  description = "Outbound SQL Server access only, to AWP-SQL-PROD"
  vpc_id      = "vpc-013b795209b52a39a"
}

resource "aws_security_group_rule" "lambda_egress_awp_sql_prod" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "AWP-SQL-PROD"
  from_port         = 1433
  to_port           = 1433
  protocol          = "tcp"
  cidr_blocks       = ["172.17.23.172/32"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

# Every default AWS security group implicitly allows this and nobody usually
# notices — DNS resolution. First attempt scoped this to the VPC CIDR
# (172.31.0.0/19), assuming Lambda queries the standard VPC+2 resolver — that
# was wrong. Confirmed via a diagnostic build reading /etc/resolv.conf inside
# the actual running container: Lambda's VPC networking routes DNS through
# its own link-local resolver proxy (169.254.100.5, in the 169.254.0.0/16
# range), NOT the VPC's own DNS resolver address. Scoped to the whole
# 169.254.0.0/16 block rather than the one observed IP, since this address
# can vary per execution environment/ENI.
resource "aws_security_group_rule" "lambda_egress_dns_udp" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "DNS resolution (UDP) to Lambda link-local resolver"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  cidr_blocks       = ["169.254.0.0/16"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

resource "aws_security_group_rule" "lambda_egress_dns_tcp" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "DNS resolution (TCP, for larger responses) to Lambda link-local resolver"
  from_port         = 53
  to_port           = 53
  protocol          = "tcp"
  cidr_blocks       = ["169.254.0.0/16"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

# The actual root cause of every DNS failure this deployment has hit: the
# VPC's DHCP option set (dopt-09a38078b8c9c2661) directs ALL name resolution
# to SG360's on-prem AD DNS servers, not the AWS-provided resolver. The
# Lambda's resolver proxy forwards queries out the ENI to those servers, and
# this security group had no rule allowing port 53 to them — so every DNS
# query silently timed out, which is why Secrets Manager, Aurora, the SQL
# Servers, S3, and the EIA API all needed (or still need) hardcoded-IP
# workarounds. These four rules open DNS to exactly the two DHCP-configured
# servers, nothing wider.
resource "aws_security_group_rule" "lambda_egress_dns_onprem_udp" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "DNS (UDP) to the VPC DHCP-configured on-prem AD resolvers"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  cidr_blocks       = ["172.17.15.109/32", "10.2.18.100/32"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

resource "aws_security_group_rule" "lambda_egress_dns_onprem_tcp" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "DNS (TCP, for larger responses) to the VPC DHCP-configured on-prem AD resolvers"
  from_port         = 53
  to_port           = 53
  protocol          = "tcp"
  cidr_blocks       = ["172.17.15.109/32", "10.2.18.100/32"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

# S3 access for the invoice-PDF bucket (sg360-bol-invoices). Both Lambda
# subnets' route tables already send the S3 prefix list through S3 Gateway
# endpoints, but gateway-endpoint traffic targets S3's PUBLIC IP ranges — the
# existing 443 rule only covers the VPC CIDR, so every S3 connection was
# blocked at this security group even before the (also broken) DNS step.
# Scoped to the managed S3 prefix list, not 0.0.0.0/0.
resource "aws_security_group_rule" "lambda_egress_s3" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "HTTPS to S3 via the VPC S3 Gateway endpoints (invoice PDF bucket)"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  prefix_list_ids   = ["pl-63a5400a"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

# Not scope creep on the prod-SQL-access promise — this is the app reaching
# its own dev-account resources (its own credential store), which was never
# something security was asked to restrict. Discovered as a real gap: this
# VPC has a Secrets Manager interface endpoint, but this security group had no
# egress allowing traffic to it, so every boto3 get_secret_value() call hung
# until timeout during Lambda init.
resource "aws_security_group_rule" "lambda_egress_secretsmanager" {
  count             = var.chg_approved_sql_access ? 1 : 0
  type              = "egress"
  description       = "AWS Secrets Manager VPC endpoint (this VPC own interface endpoint)"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["172.31.0.0/19"]
  security_group_id = aws_security_group.lambda_sql_access[0].id
}

# Split into a standalone rule specifically to avoid a dependency cycle: this
# security group can't inline-reference Aurora's security group while
# Aurora's security group inline-references this one right back. A separate
# rule resource depends on both without either security group depending on
# the other directly.
resource "aws_security_group_rule" "lambda_egress_aurora" {
  count                    = var.chg_approved_sql_access ? 1 : 0
  type                     = "egress"
  description              = "Aurora Postgres (this app own database)"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.lambda_sql_access[0].id
  source_security_group_id = aws_security_group.aurora[0].id
}

# Targets an existing security group we do not own (belongs to the
# pod-reporting team's Secrets Manager VPC endpoint) — adding our Lambda's
# security group to its allow-list, additively, without importing or managing
# that whole resource. Confirmed with the user before applying, since this
# touches infrastructure outside this project.
resource "aws_security_group_rule" "shared_secretsmanager_endpoint_ingress" {
  count                    = var.chg_approved_sql_access ? 1 : 0
  type                     = "ingress"
  description              = "HTTPS from sg360-bol-api Lambda"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = "sg-0ce10adfad6c6501e"
  source_security_group_id = aws_security_group.lambda_sql_access[0].id
}
