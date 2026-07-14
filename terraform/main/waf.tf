# Restricts the CloudFront distribution to a known IP allowlist. Promised in
# the CHG ticket as a control before anyone outside this session gets the URL.
# CLOUDFRONT-scope WAFv2 resources must live in us-east-1 — already this
# project's default region (see variables.tf), so no provider aliasing needed.
#
# Office public IPs can rotate if the ISP doesn't hand out a static one. If
# the tester loses access unexpectedly, this IP set is the first place to check.
resource "aws_wafv2_ip_set" "allowed" {
  name               = "${var.project_name}-allowed-ips"
  description        = "SG360 Corporate Wifi - covers the operator and the single external tester on the same office network"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses = [
    "198.163.183.2/32",
    "98.46.139.183/32",
    "35.174.93.154/32", # colleague's egress IP, confirmed via whatismyip.com 2026-07-14 — AWS EC2/Ashburn because their traffic exits through a corporate VPN/proxy hosted in AWS, not a home/office ISP
  ]
}

resource "aws_wafv2_web_acl" "app" {
  name        = "${var.project_name}-web-acl"
  description = "Blocks all traffic except the allowlisted office IP"
  scope       = "CLOUDFRONT"

  # Opened to allow{} 2026-07-14 — testers kept losing access as their egress
  # IPs rotated/round-robinned through a NAT gateway, and per-IP allowlisting
  # couldn't keep up. The CloudFront URL isn't linked/indexed anywhere, so this
  # is obscurity, not real access control. Flip back to block{} (the ip_set
  # rule below is left intact for that) before this app is rolled out for real
  # production use — see the CHG-ticket note above.
  default_action {
    allow {}
  }

  rule {
    name     = "allow-office-ip"
    priority = 0

    action {
      allow {}
    }

    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.allowed.arn
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-allow-office-ip"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-web-acl"
    sampled_requests_enabled   = true
  }
}
