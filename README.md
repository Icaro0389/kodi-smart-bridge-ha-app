# Kodi Smart Bridge — Home Assistant App (Pilot 0.1.0)

Repository scaffold for a custom Home Assistant app (formerly an add-on). **Do not publish publicly before review.**

The app uses TCP JSON-RPC to Kodi LXC and stores provider indexes in Home Assistant `/data/catalogs`. Host port 8790 avoids conflict with the current LXC endpoint. The app intentionally requires manual provisioning and does not modify Home Assistant integration or the LXC Kodi bridge. See `kodi-smart-bridge/README.md`.

To make this app available in the HA App Store, upload the contents of this directory to the **root** of a GitHub repository, then add its URL to the HA app repositories.
