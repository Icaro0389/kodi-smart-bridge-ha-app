# Kodi Smart Bridge — Home Assistant app (PILOT)

**Experimental, not production-ready.** Based on the user's October 2026 bridge export. The installed LXC service must remain running during validation.

## Installation

1. Create a **private** GitHub repository, place the CONTENTS of this bundle at its root.
2. Add this repository URL in Home Assistant **Settings → Apps → App Store → ⋮ → Repositories**.
3. Install **Kodi Smart Bridge (Pilot)**; do not enable automatic startup before configuration.
4. Set Kodi host and port; a new 32+ character token; IPTVXC provider endpoint and credentials in the HA app settings (never in GitHub).
5. Copy trusted TV/radio playlists into Home Assistant `/share/kodi-smart-bridge/` as `tv.m3u8` and `radio.m3u`, or change the options to other files in the shared folder.
6. Start and check logs. For manual authenticated testing, use `http://HA_IP:8790/v1/health` with `Authorization: Bearer <token>`.

**Important:** Kodi currently connects only to `127.0.0.1:9090` in the LXC. Before the remote bridge works, enable Kodi JSON-RPC TCP binding on a LAN interface and restrict port 9090 with a firewall to **only the HA IP**, or set up a secure tunnel. Do not open Kodi TCP JSON-RPC to the Internet. This pilot doesn't change your Kodi settings or firewall.

**Important:** The existing HA integration should continue pointing to the current LXC bridge on port 8787 until all media browsing, playback, and search checks succeed. A different token can be used in the pilot.

**Missing features:** The original SmartVOD index or playlist source files on Kodi are not copied into HA automatically, so some SmartVOD browsing depends on Kodi's plugin and cached state. Direct catalog download requires a reachable IPTV provider. Indexed movies and series appear after first successful refresh. Plugin:// URI playback is handled by Kodi remotely.

**Security:** Provider credentials are written to `/data/iptvxc-settings.xml` (mode 0600) inside the managed app. HA may store app options and backups containing these credentials. Protect HA admin access and backups. Do not paste options, credential-containing logs, or playlists into GitHub.

**Rollback:** Stop/uninstall the pilot; the LXC bridge remains unchanged and the dashboard continues to use its original endpoint.


## Security and deployment notice

- Never commit `/data/options.json`, API tokens, IPTV account credentials, indexed SQLite catalogs, or M3U files. Configure secrets only via Home Assistant app options.
- For newly installed instances, set `kodi_host` to the Kodi host on the trusted LAN. Default `127.0.0.1` is intentionally nonfunctional for remote Kodi.
- If your IPTV provider's VOD URLs omit a required port, configure `vod_fix_host` (provider hostname only, no scheme or credentials) and `vod_fix_port` (e.g. 24000). These are optional and replace a former provider-specific hardcoded workaround.
- Do not forward the bridge API (host port 8790), Kodi JSON-RPC (9090), or the application to the public Internet. Bridge bearer authentication does **not** encrypt HTTP; use only on a trusted LAN or secure private tunnel.
- Git retains previous commits: the initial public history included a private-LAN IP and provider hostname in code, but no embedded account secrets were identified. If you consider even those references sensitive, rewrite Git history / recreate the repository before distribution.
- This app is still a **pilot**. Existing installed app settings are unchanged by repository commits. Test a fresh install on a nonconflicting port before any cutover; do not replace the working app without backup.
