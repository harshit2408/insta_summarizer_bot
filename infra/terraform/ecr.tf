# -----------------------------------------------------------------------------
# ECR Private -- container registry for the Content Extractor Lambda
#
# Lambda can ONLY pull from private ECR repositories in the same AWS account.
# ECR Public is not supported as a Lambda image source (AWS limitation).
#
# Cost note: intra-region pulls (Lambda -> ECR, same region) are FREE.
# Storage is ~$0.10/GB/month. A 3-4 GB image costs ~$0.35/month.
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "content_extractor" {
  name                 = "${var.project_name}-${var.environment}-content-extractor"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${var.project_name}-${var.environment}-content-extractor" }
}

# Keep only the last image to minimise storage cost
resource "aws_ecr_lifecycle_policy" "content_extractor" {
  repository = aws_ecr_repository.content_extractor.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the latest image to minimise storage cost"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 1
        }
        action = { type = "expire" }
      }
    ]
  })
}
