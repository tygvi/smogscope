# Inputs: values that may change, kept out of the resource definitions.
# Set them in terraform.tfvars (git-ignored) or accept the defaults.

variable "region" {
  description = "AWS region. This account's project is locked to Sydney (design-qa Q46)."
  type        = string
  default     = "ap-southeast-2"
}

variable "my_ip" {
  description = "Your home IP, the only address allowed to SSH in. Find it: curl -s https://checkip.amazonaws.com"
  type        = string
}

variable "instance_type" {
  description = "Server size: t4g.micro = 2 ARM vCPU, 1 GB RAM (~$12/month with disk and IP)."
  type        = string
  default     = "t4g.micro"
}
