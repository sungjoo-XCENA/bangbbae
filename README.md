# Bangbbae

Dev server resource monitoring dashboard with per-user fair-share alerts and Teams notifications.

<img width="1424" height="1371" alt="image" src="https://github.com/user-attachments/assets/35723669-3b9b-4d7c-8c9e-190f1ebb940b" />
<img width="436" height="420" alt="image" src="https://github.com/user-attachments/assets/f6df3897-8b55-4f95-bef7-dad973f5b0a8" />
<img width="502" height="290" alt="image" src="https://github.com/user-attachments/assets/38b8d2ce-028a-4591-8e70-72c94ff87367" />

## Features

- Auto-detect users from `/home/` directory
- Active / Inactive user grouping
- Per-user CPU, RAM, Disk usage with fair-share (N-way split) calculation
- Warning / Critical alerts based on fair-share threshold
- Microsoft Teams webhook notifications with cooldown
- Real-time dashboard (auto-refresh)
- Web-based settings (webhook URL, thresholds, refresh interval)

## Quick Start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo venv/bin/python3 app.py -p 8000
```

Access: `http://<server-ip>:8000`

### Background mode

```bash
nohup sudo venv/bin/python3 app.py -p 8000 > server.log 2>&1 &
```

## Usage

```
python3 app.py [-p PORT] [--host HOST]

Options:
  -p, --port    Port to bind (default: 8000)
  --host        Host to bind (default: 0.0.0.0)
```

## Settings

Click **Settings** button on the dashboard to configure:

| Setting | Description | Default |
|---------|-------------|---------|
| Teams Webhook URL | Power Automate workflow URL | - |
| Warning (% of fair share) | Warning alert threshold | 120% |
| Critical (% of fair share) | Critical alert threshold | 150% |
| Alert Cooldown | Min between same alerts | 5 min |
| Refresh Interval | Dashboard polling interval | 3 sec |
| Exclude Users | Users to hide from monitoring | - |

## Fair Share

Resources are split equally among users:

- **CPU / RAM**: divided by **active** user count (users with running processes)
- **Disk**: divided by **all** `/home/` user count (disk persists regardless of activity)

## Teams Alert

When a user exceeds their fair share threshold, an Adaptive Card is sent to the configured Teams chat:

```
⚠️ Server Resource Warning
User:     sungjoo
Resource: CPU
Current:  102% (fair: 933.3%)
Time:     2026-04-10 14:55:00
[Open Dashboard]
```

## Requirements

- Python 3.8+
- `sudo` recommended for full visibility of all users' processes and disk
- Teams webhook URL (Power Automate Workflows)
