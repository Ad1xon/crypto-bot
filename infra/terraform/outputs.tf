output "server_ipv4" {
  description = "Public IPv4 of the bot host."
  value       = hcloud_server.bot.ipv4_address
}

output "server_ipv6" {
  description = "Public IPv6 of the bot host."
  value       = hcloud_server.bot.ipv6_address
}

output "ssh_command" {
  description = "SSH into the host."
  value       = "ssh deploy@${hcloud_server.bot.ipv4_address}"
}

output "grafana_tunnel" {
  description = "Forward Grafana/Prometheus/Alertmanager to localhost (UIs are not exposed publicly)."
  value       = "ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 deploy@${hcloud_server.bot.ipv4_address}"
}

output "data_volume_device" {
  description = "Linux device path of the persistent data volume."
  value       = hcloud_volume.data.linux_device
}
