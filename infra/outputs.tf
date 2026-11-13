# Values Terraform prints after `apply` (and with `terraform output`).

output "public_ip" {
  description = "The permanent (Elastic) IP: open http://<this> in a browser."
  value       = aws_eip.web.public_ip
}

output "ssh_command" {
  value = "ssh -i ~/.ssh/smogscope-key ubuntu@${aws_eip.web.public_ip}"
}
