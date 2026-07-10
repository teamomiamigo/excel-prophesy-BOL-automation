---
description: Temporarily expose the locally-running SG360 BOL app to the public internet via a Cloudflare quick tunnel, so one external tester can reach it with real live data when the SG360 internal network won't route between machines (confirmed segmented — see plan file).
---

# /cloudflare — Temporary public tunnel for external testing

## When to use this

Only when someone needs to test the app with **real, live data** and can't reach it over the internal SG360 network (confirmed: the corporate network isolates client machines from each other at the infrastructure level — `Test-NetConnection` between two machines on the same network fails with `DestinationNetworkUnreachable`; no local firewall/Windows setting fixes this). This is a stopgap for one-off testing, not a deployment — the app must still be running on this machine the whole time.

## ⚠️ Security — read before running

This exposes whatever is running on `localhost:3000` (the real dashboard, with real BOL/invoice/SQL Server data) on a **public internet URL** — anyone with the link can access it, not just your intended tester. Quick tunnels (the fast, no-signup mode used here) have **no built-in password protection** — that requires a full Cloudflare account + Access policy, which defeats the point of doing this quickly.

Mitigations actually available:
- The URL is a long random string (`https://<random-words>.trycloudflare.com`) — not guessable, but not secret either if it leaks.
- **Keep the tunnel open only for the duration of the actual test.** Close it the moment testing is done — don't leave it running.
- Never reuse the same tunnel URL across multiple testing sessions; start a fresh one each time.

Always get explicit confirmation from the user before running this — do not treat a general "let's deploy" or "let's test" request as authorization to expose real data publicly. This specific action needs its own explicit yes.

## Steps

1. **Confirm the app is already running locally** (backend on :8000, frontend on :3000) — use the `/run` skill first if not, or check via `docker ps`-style checks: `netstat -ano | findstr ":3000"` should show `0.0.0.0:3000 LISTENING`.

2. **Install cloudflared if not already present:**
   ```powershell
   winget install --id Cloudflare.cloudflared --silent --accept-package-agreements --accept-source-agreements
   ```

3. **Start the quick tunnel** (foreground process — keep it running for the test session):
   ```powershell
   cloudflared tunnel --url http://localhost:3000
   ```
   Watch the output for a line like:
   ```
   https://some-random-words.trycloudflare.com
   ```
   That's the URL to share.

4. **Verify it actually works** before handing it off — load the URL yourself first, confirm the dashboard renders with real data, not an error page.

5. **Share the URL** with the tester. Remind them this is a temporary test link, not the permanent app.

6. **Shut it down the moment testing is done** — `Ctrl+C` in the terminal running `cloudflared`, or stop the process. Confirm it's actually closed:
   ```powershell
   Get-Process cloudflared -ErrorAction SilentlyContinue
   ```
   Should return nothing once stopped.

## Known limitations

- Tunnel dies if this machine sleeps, locks aggressively, loses network, or the terminal running it closes — no persistence.
- No authentication layer — see security section above.
- Free/quick-tunnel URLs are not stable — a new one is generated every time you start it, so don't bookmark/reuse them.
