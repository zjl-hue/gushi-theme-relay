# gushi-theme-relay

Public relay for the Gushi theme snapshot. GitHub Actions fetches the EastMoney
concept catalogue on weekdays and publishes the validated snapshot at:

`https://raw.githubusercontent.com/zjl-hue/gushi-theme-relay/main/public-data/themes/latest.json`

The cloud server imports that JSON through the `gushi-theme-relay.service`
systemd unit. The workflow can also be started manually from the **Actions**
tab with **Publish theme snapshot → Run workflow**.
