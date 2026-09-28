# Cloud deployment — Oracle Always Free VM

The pipeline runs on a small always-on Linux server instead of Frank's PC.
Nothing on the PC matters anymore except one-time file copies.

## 0. What you need (one-time, ~20 min)

- A Google account for Oracle Cloud signup (a card is required for
  verification but the Always Free tier is never charged).
- Frank's PC ON for two minutes to copy 3 files to the server.

## 1. Create the VM (console.oracle.com)

1. Sign up at <https://cloud.oracle.com> → Home region: pick the closest
   (e.g. Johannesburg or any US region — Johannesburg stock is limited, if
   the launch fails with "out of capacity", retry later or pick another
   home region at signup).
2. Menu → Compute → Instances → **Create instance**.
3. Name: `ghostdirector`. Image: **Ubuntu 22.04** (or 24.04). Shape:
   **VM.Standard.E2.1.Micro** (1 OCPU / 1 GB RAM — Always Free).
   ⚠️ 1 GB is tight for the render; if the OOM shows up, switch to
   **A1.Flex (Ampere)**: 4 OCPU / 24 GB is also Always Free and roomier —
   try Ampere FIRST, availability permitting.
4. SSH keys: **Paste a public key** → paste the key from step 2 below
   (or generate one now with `ssh-keygen -t ed25519`).
5. Create. Note the **Public IP address**.

## 2. Upload the project (from Frank's PC, project root)

```bash
# one-time SSH key (skip if you have one)
ssh-keygen -t ed25519 -f ~/.ssh/oracle_gd -N ""

# copy the project (code + assets + templates; output/ stays local-only)
scp -i ~/.ssh/oracle_gd -r \
  main.py config.py models.py requirements.txt \
  pipeline storage templates utils assets docs deploy \
  ubuntu@SERVER_IP:/opt/ghostdirector/

# copy the secrets
scp -i ~/.ssh/oracle_gd \
  .env client_secrets.json youtube_token.famefiles.json \
  ubuntu@SERVER_IP:/opt/ghostdirector/
```

(Sudo-create /opt/ghostdirector first: `ssh ubuntu@IP "sudo mkdir -p /opt/ghostdirector && sudo chown ubuntu /opt/ghostdirector"`.)

## 3. Provision + schedule (on the server)

```bash
ssh -i ~/.ssh/oracle_gd ubuntu@SERVER_IP
cd /opt/ghostdirector
bash deploy/setup_server.sh          # deps + venv + smoke test

# systemd units (timer fires 08:00 Harare = 06:00 UTC, persistent catch-up)
sudo cp deploy/ghostdirector-daily.service /etc/systemd/system/
sudo cp deploy/ghostdirector-daily.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ghostdirector-daily.timer
systemctl list-timers ghostdirector-daily.timer   # shows next fire time
```

## 4. Verify before you walk away

```bash
# dry run of the whole entry point (idempotent: today-published → no-op)
/opt/ghostdirector/venv/bin/python /opt/ghostdirector/main.py --daily

tail -20 /opt/ghostdirector/output/_daily.log
```

Then leave the VM running (Always Free includes it 24/7). Each day at
08:00 Harare it picks a fresh story, renders short + doc, uploads BOTH
unlisted with the AI box ticked, and logs everything to
`/opt/ghostdirector/output/_daily.log`. You review + publish from your
phone whenever you like — nothing is ever public without you.

## 5. Operations cheat-sheet

```bash
systemctl list-timers ghostdirector-daily.timer  # when does it fire next
journalctl -u ghostdirector-daily.service -n 100 # last run's logs
tail -f /opt/ghostdirector/output/_daily.log     # live run
systemctl start  ghostdirector-daily.service     # run NOW (manual)
systemctl status ghostdirector-daily.service     # last exit code
```

## Notes

- **No-repeat ledger lives on the server** (`output/…`) — the server's
  history is what stops story repeats; your PC's old history is not copied.
- **OAuth token**: the famefiles token file copied to the server works as-is
  (same Google project, same client). It still carries the Testing-mode
  7-day expiry until the Google Console Branding → Publish flip is done —
  after that it never expires and the unattended flow is fully hands-off.
- **DB**: SQLite at `output/ghostdirector.db` on the server (backup =
  scp it down occasionally).
- If the 1 GB micro VM OOMs on the doc render, the FIX-068 bounded-pass
  assembler (≤14 inputs/pass) is already the mitigation; the real fix is
  the Ampere A1 shape (4 OCPU/24 GB, also free).
