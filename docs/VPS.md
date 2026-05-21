# VPS Deployment Quickstart

This is the canonical way to run the bot. Cost: **$5/month**. Time to deploy: **10 minutes**.

## 1. Provision a VPS

Pick one — both are cheap enough that it doesn't matter:

| Provider | Plan | Specs | Cost |
|---|---|---|---|
| **Hetzner CX22** *(recommended)* | EU/US data centers | 2 vCPU, 4 GB RAM, 40 GB SSD | $4.55/mo |
| DigitalOcean basic | NYC/SFO/FRA | 1 vCPU, 1 GB RAM, 25 GB SSD | $6/mo |
| Vultr Cloud Compute | global | 1 vCPU, 1 GB RAM | $5/mo |

Choose Ubuntu 24.04 (or 22.04) as the image. Add your SSH key during signup so you can log in without a password.

## 2. SSH in

```bash
ssh root@<YOUR_VPS_IP>
```

## 3. Run the install script

One command. It installs Python, clones the repo, creates a service user, sets up systemd, and starts all 4 agents.

```bash
curl -fsSL https://raw.githubusercontent.com/Arnavtaduvayi/prediction-market-analyst/main/scripts/deploy.sh | sudo bash
```

After ~3 minutes you'll see a "DEPLOY COMPLETE" banner. The bot is now running paper trades 24/7.

## 4. Verify

```bash
# Are the 4 agents running?
systemctl status predmkt-scanner predmkt-brain predmkt-executor predmkt-exit

# Live log of any agent
journalctl -u predmkt-exit -f

# Current paper P&L
sudo -u predmkt /opt/predmkt/.venv/bin/python /opt/predmkt/paper_cross.py status
```

If all 4 services show "active (running)" you're done with paper mode.

## 5. Going live (real Kalshi orders)

Paper trading works without credentials. Live trading needs your Kalshi API key:

```bash
# From your LAPTOP, copy the private key to the VPS:
scp ~/Documents/GitHub/prediction-market-analyst/keys/kalshi_private.pem \
    root@<VPS_IP>:/opt/predmkt/keys/

# On the VPS, set the key ID + permissions:
ssh root@<VPS_IP>
echo "KALSHI_API_KEY_ID=your-uuid-here" > /opt/predmkt/.env
chown predmkt:predmkt /opt/predmkt/.env /opt/predmkt/keys/kalshi_private.pem
chmod 600 /opt/predmkt/.env /opt/predmkt/keys/kalshi_private.pem

# Restart so the executor picks up the creds:
systemctl restart predmkt-executor predmkt-exit
```

> **The current code still uses paper mode even with creds present.** Going live requires a config flag flip I'll add when you confirm you're ready. Paper trades for at least 7 days first.

## 6. Optional — let the VPS push journal updates back to GitHub

So you can check progress from your phone without SSH-ing into the VPS:

```bash
# On the VPS, generate a deploy key:
sudo -u predmkt ssh-keygen -t ed25519 -f /home/predmkt/.ssh/id_ed25519 -N ""

# Cat the public key:
cat /home/predmkt/.ssh/id_ed25519.pub
```

Add that key as a **Deploy Key with write access** at:
`https://github.com/Arnavtaduvayi/prediction-market-analyst/settings/keys/new`

Then switch the repo to SSH remote:
```bash
sudo -u predmkt git -C /opt/predmkt remote set-url origin \
  git@github.com:Arnavtaduvayi/prediction-market-analyst.git
```

The hourly commit timer (`predmkt-commit.timer`) will now push state snapshots automatically.

## Operational cheatsheet

```bash
# Watch all agents at once
journalctl -u predmkt-scanner -u predmkt-brain -u predmkt-executor -u predmkt-exit -f

# Restart all agents (e.g. after a code update)
cd /opt/predmkt && sudo git pull && sudo systemctl restart predmkt-{scanner,brain,executor,exit}

# Stop everything
sudo systemctl stop predmkt-{scanner,brain,executor,exit}

# Force a target refresh
sudo systemctl start predmkt-targets.service

# Cancel all open paper positions
sudo -u predmkt /opt/predmkt/.venv/bin/python /opt/predmkt/paper_cross.py cancel manual

# Tail just exits (to see when trades close)
journalctl -u predmkt-exit -f --output=cat
```

## Agent polling intervals

| Agent | Interval | Why |
|---|---|---|
| Scanner | 300 s (5 min) | Kalshi market list changes slowly |
| Brain | 360 s (6 min) | Polymarket equivalent check is the slowest step |
| Executor | 420 s (7 min) | Reads thesis output a bit after brain runs |
| Exit monitor | **60 s (1 min)** | Critical for catching volume spikes in real time |
| Targets | weekly (Sun 12:37 UTC) | Whale list is stable |
| Commit | hourly (\*:23 UTC) | Snapshot to GitHub |

All intervals can be tuned in the systemd `.service` files. After editing, run `sudo systemctl daemon-reload && sudo systemctl restart predmkt-<name>`.
