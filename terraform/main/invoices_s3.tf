# Storage for uploaded ALG invoice PDFs, keyed by Z-number (e.g. "Z558465.pdf").
# Lambda has no route to the on-prem UNC file share (INVOICE_FOLDER) — this
# bucket is the actual mechanism behind "click the Z-number to view the PDF"
# in Lambda mode. Private, no public access; Lambda gets a presigned URL to
# read it back and redirects the browser there rather than proxying the
# file's bytes through itself.
resource "aws_s3_bucket" "invoices" {
  bucket = "${var.project_name}-invoices"
}

resource "aws_s3_bucket_public_access_block" "invoices" {
  bucket                  = aws_s3_bucket.invoices.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "lambda_invoices_s3_access" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.invoices.arn}/*"]
  }
}

resource "aws_iam_role_policy" "lambda_invoices_s3_access" {
  name   = "${var.project_name}-lambda-invoices-s3-access"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_invoices_s3_access.json
}

output "invoices_bucket_name" {
  value = aws_s3_bucket.invoices.bucket
}
