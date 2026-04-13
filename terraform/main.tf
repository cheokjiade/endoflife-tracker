terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ──────────────────────────────────────────────
# S3 — config bucket
# ──────────────────────────────────────────────

resource "aws_s3_bucket" "config" {
  bucket = var.config_bucket_name
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket = aws_s3_bucket.config.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "eol_config" {
  bucket       = aws_s3_bucket.config.id
  key          = "eol_config.json"
  source       = "${path.module}/../eol_config.json"
  etag         = filemd5("${path.module}/../eol_config.json")
  content_type = "application/json"
}

# ──────────────────────────────────────────────
# SNS — email notifications
# ──────────────────────────────────────────────

resource "aws_sns_topic" "eol_alerts" {
  name = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.eol_alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# ──────────────────────────────────────────────
# IAM — Lambda execution role
# ──────────────────────────────────────────────

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}:*"
      },
      {
        Sid    = "S3ReadConfig"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.config.arn}/${aws_s3_object.eol_config.key}"
      },
      {
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = aws_sns_topic.eol_alerts.arn
      },
      {
        Sid    = "SESSendEmail"
        Effect = "Allow"
        Action = ["ses:SendEmail"]
        Resource = "*"
      },
    ]
  })
}

# ──────────────────────────────────────────────
# Lambda function
# ──────────────────────────────────────────────

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../lambda_function.py"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "eol_checker" {
  function_name    = var.project_name
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      CONFIG_BUCKET  = aws_s3_bucket.config.id
      CONFIG_KEY     = aws_s3_object.eol_config.key
      SNS_TOPIC_ARN  = aws_sns_topic.eol_alerts.arn
      SES_FROM_EMAIL = var.ses_from_email
      SES_TO_EMAILS  = var.ses_to_emails
    }
  }
}

# ──────────────────────────────────────────────
# CloudWatch Events — daily schedule
# ──────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.project_name}-daily"
  description         = "Trigger EOL checker Lambda on a daily schedule"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = var.project_name
  arn       = aws_lambda_function.eol_checker.arn
}

resource "aws_lambda_permission" "cloudwatch" {
  statement_id  = "AllowCloudWatchInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.eol_checker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}
