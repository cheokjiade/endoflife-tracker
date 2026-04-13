variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-west-1"
}

variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "eol-checker"
}

variable "config_bucket_name" {
  description = "S3 bucket name for the EOL config file (must be globally unique)"
  type        = string
}

variable "notification_email" {
  description = "Email address to receive EOL alerts (must confirm the SNS subscription)"
  type        = string
}

variable "schedule_expression" {
  description = "CloudWatch Events schedule expression for the daily run"
  type        = string
  default     = "cron(0 8 * * ? *)" # 8:00 AM UTC daily
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 60
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 128
}

variable "ses_from_email" {
  description = "SES sender email address (must be verified in SES). Leave empty to skip SES."
  type        = string
  default     = ""
}

variable "ses_to_emails" {
  description = "Comma-separated list of recipient emails for SES. Leave empty to skip SES."
  type        = string
  default     = ""
}
