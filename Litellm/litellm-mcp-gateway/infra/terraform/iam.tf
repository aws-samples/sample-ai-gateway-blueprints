data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# Execution role: pull the image, write logs, inject the secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "ReadGatewaySecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }

  statement {
    sid       = "DecryptGatewaySecrets"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.this.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "gateway-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# Task role: what LiteLLM itself may call. Only the pinned config object and the listed Bedrock
# inference profiles. No Atlassian credential lives in AWS IAM; per-user tokens are in the database.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ReadPinnedConfig"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.config.arn}/${aws_s3_object.config.key}"]
  }

  statement {
    sid       = "DecryptConfig"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.this.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "InvokeInferenceProfiles"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.inference_profile_arns
  }

  statement {
    sid       = "InvokeModelsThroughProfiles"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.foundation_model_arns

    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values   = local.inference_profile_arns
    }
  }
}

data "aws_iam_policy_document" "task_guardrails" {
  count = length(var.bedrock_guardrail_arns) > 0 ? 1 : 0

  statement {
    sid       = "ApplyGuardrails"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = var.bedrock_guardrail_arns
  }
}

resource "aws_iam_role_policy" "task_guardrails" {
  count  = length(var.bedrock_guardrail_arns) > 0 ? 1 : 0
  name   = "bedrock-guardrails"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_guardrails[0].json
}

resource "aws_iam_role_policy" "task" {
  name   = "gateway"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
