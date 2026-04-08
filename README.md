# Cursor to Ramp Receipt Sync

Automatically sync your [Cursor](https://cursor.com) billing invoices to [Ramp](https://ramp.com) as transaction receipts. No more manually downloading PDFs and uploading them.

Works standalone via CLI or hands-free with [Craft Agent](https://craft.do/agents) using the included skill.

## How It Works

1. Queries Ramp for Cursor transactions missing receipts
2. Matches them to Cursor invoices via the Cursor billing API (or downloads directly from Stripe when the API token is expired)
3. Downloads the invoice PDFs
4. Uploads them to Ramp as receipts

Idempotency keys prevent duplicate uploads, so it's always safe to re-run.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/cursor-ramp-receipts.git
cd cursor-ramp-receipts

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Fill in your `.env`:

| Variable | Required | Description | How to get |
|----------|----------|-------------|------------|
| `RAMP_CLIENT_ID` | Yes | Ramp OAuth client ID | Ramp Dashboard > Company > Developer API |
| `RAMP_CLIENT_SECRET` | Yes | Ramp OAuth client secret | Same as above |
| `RAMP_USER_ID` | Yes | Your Ramp user UUID | Ramp API or MCP `load_users` |
| `CURSOR_SESSION_TOKEN` | Optional | WorkosCursorSessionToken cookie | Browser DevTools (HttpOnly, not readable via JS) |
| `CURSOR_TEAM_ID` | Yes | Your Cursor team ID | Network tab on cursor.com/dashboard |

See [docs/ramp-setup.md](docs/ramp-setup.md) for detailed Ramp API setup.

> **Note:** The Cursor session token is optional. When it's expired or missing, the Craft Agent skill falls back to downloading invoices directly from Stripe via the browser.

### 3. Run

```bash
# Check for missing receipts
python sync_engine.py find-missing --days 90

# Preview what would be synced
python sync_engine.py sync --dry-run --days 90

# Run the sync
python sync_engine.py sync --days 90
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `check-token` | Verify Cursor session token is valid |
| `update-token <token>` | Update the session token in .env |
| `find-missing --days N` | List Ramp transactions missing receipts |
| `list-invoices` | Fetch all Cursor invoices via API |
| `match --days N` | Match missing transactions to invoices |
| `sync --days N [--dry-run]` | Download PDFs and upload to Ramp |
| `upload-receipt --transaction-id X --receipt-path Y` | Upload a single receipt |

All commands output structured JSON to stdout. Logs go to stderr. Pipe stderr to `/dev/null` for clean JSON parsing.

## Using with Craft Agent

[Craft Agent](https://craft.do/agents) can run this fully autonomously, including browser-based invoice downloads when your Cursor API token is expired.

### Install the skill

Copy `skill/SKILL.md` to your Craft Agent workspace:

```bash
cp skill/SKILL.md ~/.craft-agent/workspaces/<your-workspace>/skills/cursor-receipts/SKILL.md
```

Edit the paths in `SKILL.md` to point to where you cloned this repo.

### Run on demand

In Craft Agent, type:

```
/cursor-receipts
```

The agent will check for missing receipts, download any missing invoice PDFs (via API or browser fallback), and upload them to Ramp.

### Schedule it

Create a [Craft Agent automation](https://craft.do/agents) to run the skill on a schedule (e.g., weekly) so receipts stay synced without any manual effort.

## Project Structure

```
.
├── sync_engine.py          # Main Cursor sync engine (CLI)
├── cursor_ramp_sync.py     # Legacy Cursor sync (standalone)
├── requirements.txt        # Python dependencies
├── .env.example            # Credential template
├── docs/                   # Setup guides
├── skill/                  # Craft Agent skill definition
│   └── SKILL.md
└── receipts/               # Downloaded PDFs (git-ignored)
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Cursor token expired | Use Craft Agent (browser fallback) or refresh manually |
| Stripe PDF download fails | Try "Download invoice" instead of "Download receipt" (avoids hCaptcha) |
| Ramp upload 409 | Receipt already uploaded (idempotency key). Safe to skip. |
| No matches found | Widen `--days` range or check for amount mismatches (tax/refunds) |

## License

MIT
