# Deploying updates (data-preserving)

Live data (`/opt/intranet/data/intranet.db`, `data/generated`, `data/uploads`) is never touched.

1. **Snapshot** (automatic daily snapshots at 03:00 UTC are enabled; take a manual one before a deploy):
   Lightsail console → Instances → demeisen-intranet → Snapshots → *Create snapshot*.
2. **Open browser SSH** (Lightsail's console IPs only): `python3 deploy/console_ssh.py open`
   (or in the console Networking tab add SSH restricted to "Lightsail browser SSH/RDP").
3. In the Lightsail console click **Connect using SSH** and paste:
   ```
   curl -fsSL https://raw.githubusercontent.com/HitmanH/-demeisen-intranet-poc/main/deploy/update.sh | sudo bash
   ```
   It backs up code + a DB copy to `/opt/intranet-backups/<time>`, installs the new code,
   restarts `intranet`, and rolls the code back automatically if the app doesn't come up.
4. **Close SSH again**: `python3 deploy/console_ssh.py close`.

Once the script is on the server, later updates can also be run as `sudo /opt/intranet/deploy/update.sh`.
