#!/bin/bash
# EC2 "user data": AWS runs this ONCE, as root, the first time the server boots.
# It prepares a fresh Ubuntu 24.04 machine to run SmogScope:
#   1. 2 GB swap   (t4g.micro has only 1 GB RAM; building the image needs more)
#   2. Docker + the Compose plugin, from Docker's official apt repository
#   3. lets the "ubuntu" user run docker without sudo
# Progress/errors are logged on the server at /var/log/cloud-init-output.log
set -euxo pipefail

# --- 1. Swap: disk space used as emergency RAM -------------------------------
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile                       # only root may read it (it can hold memory contents)
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab   # keep it after reboots
fi

# --- 2. Docker ----------------------------------------------------------------
apt-get update
apt-get install -y ca-certificates curl git

# Trust Docker's signing key, then add their package repository
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker               # start Docker now and on every boot

# --- 3. Let the normal login user use docker ------------------------------------
usermod -aG docker ubuntu

echo "SmogScope server setup finished"
