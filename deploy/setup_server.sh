#!/usr/bin/env bash
# GhostDirector server setup (Ubuntu 22.04/24.04, Oracle Always Free VM).
# Run ONCE on the server, from the project root:
#   bash deploy/setup_server.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== GhostDirector server setup =="

# 1. System deps: ffmpeg (all audio/video work) + venv tooling.
sudo apt-get update -y
sudo apt-get install -y ffmpeg python3-venv python3-pip

# 2. Python environment (mirror of the Windows venv).
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Runtime layout. output/ holds the DB, ledgers and finished renders —
#    it must SURVIVE redeploys, so it is never overwritten by git.
mkdir -p output

# 4. Secrets layout (copy from local machine, see deploy/README.md):
#      .env                          <- API keys
#      client_secrets.json           <- OAuth client
#      youtube_token.famefiles.json  <- YouTube consent token
#    config.py already loads .env via python-dotenv, so no env vars needed.
for f in .env client_secrets.json youtube_token.famefiles.json; do
    if [ -f "$f" ]; then echo "OK: $f present"
    else echo "MISSING: $f (scp it from your PC — see deploy/README.md)"; fi
done

# 5. Smoke test: offline-capable import + ffmpeg presence.
./venv/bin/python -c "import config, main; print('imports OK')"
ffmpeg -version | head -1

echo
echo "== Setup complete. Next: deploy/README.md step 3 (enable the timer). =="
