# secrets/

Files here are mounted into containers as Docker secrets and are gitignored.

| File                  | Used by      | Content                         |
|-----------------------|--------------|---------------------------------|
| `discord_webhook_url` | Alertmanager | Discord webhook URL, one line   |

```bash
echo "https://discord.com/api/webhooks/..." > secrets/discord_webhook_url
```
