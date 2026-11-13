# SmogScope's AWS infrastructure, as code.
#
# These resources already existed (created with the AWS CLI), so `imports.tf`
# adopts them. After import, `terraform plan` must show no changes.
#
# Syntax of one resource:
#
#   resource "<type>" "<our name for it>" {
#     argument = value
#     nested_block {
#       argument = value
#     }
#   }
#
# Docs for every type: https://registry.terraform.io/providers/hashicorp/aws/latest/docs


# 1. FIREWALL ----------------------------------------------------------------------
# HTTP/HTTPS for the world; SSH only from the current home IP.
resource "aws_security_group" "web" {
  name        = "smogscope-web"
  description = "SmogScope: HTTP for all, SSH for me"   # changing this replaces the group
  vpc_id      = "vpc-081b8a6cb3c35e5e5"

  ingress {
    description = "SSH from home"
    protocol    = "tcp"
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["${var.my_ip}/32"]
  }

  ingress {
    description = "HTTP (Caddy redirects to HTTPS)"
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS via Caddy"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS/3 (QUIC)"
    protocol    = "udp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    # AWS adds "allow all" by default, but Terraform removes rules it doesn't see.
    description = "All outbound"
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}


# 2. SSH KEY -----------------------------------------------------------------------
# Only the PUBLIC half goes to AWS; the private key stays on the Mac.
resource "aws_key_pair" "smogscope" {
  key_name   = "smogscope-key"
  public_key = file(pathexpand("~/.ssh/smogscope-key.pub"))

  # The server was launched with this key; never let a local file difference
  # replace it (that would break SSH access).
  lifecycle {
    ignore_changes = [public_key]
  }
}


# 3. SERVER --------------------------------------------------------------------------
# DANGER: changing `ami` or `user_data` on an existing server makes Terraform
# DESTROY and recreate it (all data lost). `ignore_changes` protects against that.
resource "aws_instance" "smogscope" {
  ami                    = "ami-0b58dc56768a169ca"
  instance_type          = var.instance_type
  key_name               = aws_key_pair.smogscope.key_name
  subnet_id              = "subnet-03c6a53bf838fbbd7"
  vpc_security_group_ids = [aws_security_group.web.id]
  user_data              = file("${path.module}/../deploy/user-data.sh")

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"   # IMDSv2 only (Q46)
  }

  tags = {
    Name = "smogscope"
  }

  lifecycle {
    ignore_changes = [ami, user_data]
  }
}


# 4. PERMANENT IP --------------------------------------------------------------------
# The Elastic IP survives stop/start, so DNS and the TLS certificate stay valid.
resource "aws_eip" "web" {
  domain   = "vpc"
  instance = aws_instance.smogscope.id
}
