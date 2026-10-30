locals {
  name = "${var.project}-${var.environment}"

  labels = {
    project     = var.project
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "hcloud_ssh_key" "deploy" {
  name       = "${local.name}-deploy"
  public_key = file(pathexpand(var.ssh_public_key_path))
  labels     = local.labels
}

# Inbound: SSH from admin CIDRs + ICMP only. Grafana/Prometheus are bound to
# 127.0.0.1 on the host and reached through an SSH tunnel, so no web port is exposed.
resource "hcloud_firewall" "bot" {
  name   = "${local.name}-fw"
  labels = local.labels

  rule {
    description = "SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_cidrs
  }

  rule {
    description = "ICMP"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

# Tick data outlives the server: rebuilding/replacing the VM never loses the dataset.
resource "hcloud_volume" "data" {
  name              = "${local.name}-data"
  size              = var.data_volume_size_gb
  location          = var.location
  format            = "ext4"
  delete_protection = var.enable_protection
  labels            = local.labels
}

resource "hcloud_server" "bot" {
  name               = local.name
  server_type        = var.server_type
  image              = var.image
  location           = var.location
  ssh_keys           = [hcloud_ssh_key.deploy.id]
  firewall_ids       = [hcloud_firewall.bot.id]
  backups            = var.enable_backups
  delete_protection  = var.enable_protection
  rebuild_protection = var.enable_protection
  labels             = local.labels

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ssh_public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
    volume_device  = hcloud_volume.data.linux_device
    repo_url       = var.repo_url
    repo_ref       = var.repo_ref
  })

  lifecycle {
    # cloud-init only runs on first boot; editing it must not silently replace the VM.
    ignore_changes = [user_data, ssh_keys, image]
  }
}

resource "hcloud_volume_attachment" "data" {
  volume_id = hcloud_volume.data.id
  server_id = hcloud_server.bot.id
  automount = false
}
