output "lambda_function_name" {
  description = "Name of the deployed Lambda function"
  value       = aws_lambda_function.eol_checker.function_name
}

output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function"
  value       = aws_lambda_function.eol_checker.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for EOL alerts"
  value       = aws_sns_topic.eol_alerts.arn
}

output "config_bucket" {
  description = "S3 bucket holding the EOL config file"
  value       = aws_s3_bucket.config.id
}

output "config_file_key" {
  description = "S3 key of the config file — update this file to change tracked products"
  value       = aws_s3_object.eol_config.key
}
