# ─────────────────────────────────────────────────────────────────────────────
# Monitoring — billing alarm + SNS topic for alerts
# ─────────────────────────────────────────────────────────────────────────────

# SNS topic — receives all project alarms
resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-${var.environment}-alerts"
}

# Email subscription (manual confirmation required after `terraform apply`)
resource "aws_sns_topic_subscription" "email_alerts" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Billing alarm — fires when estimated charges exceed threshold.
# NOTE: billing metrics are only available in us-east-1.
resource "aws_cloudwatch_metric_alarm" "billing" {
  count               = var.aws_region == "us-east-1" ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-billing-alert"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 86400   # 24 hours
  statistic           = "Maximum"
  threshold           = var.billing_alert_threshold_usd
  alarm_description   = "Alert when AWS bill exceeds $${var.billing_alert_threshold_usd}"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    Currency = "USD"
  }
}
