# ADOPT the resources we created by hand with the AWS CLI, instead of creating
# duplicates. `terraform plan` reads these blocks and says "N to import".
# Once `terraform apply` has imported them, these blocks can be deleted.
#
# IDs come from:
#   aws ec2 describe-security-groups --group-ids sg-0828b218b9add3a63 ...
#   aws ec2 describe-instances ...
#   aws ec2 describe-addresses ...

import {
  to = aws_security_group.web
  id = "sg-0828b218b9add3a63"
}

import {
  to = aws_key_pair.smogscope
  id = "smogscope-key"
}

import {
  to = aws_instance.smogscope
  id = "i-0b2edfaedbd1e997b"
}

import {
  to = aws_eip.web
  id = "eipalloc-01c9027d641b4bf21"
}
