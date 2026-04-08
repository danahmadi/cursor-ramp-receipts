---
name: "Cursor Receipts"
description: "Sync Cursor billing invoices to Ramp as receipts -- find missing receipts, download PDFs, and upload them"
requiredSources:
  - ramp
alwaysAllow:
  - Bash
  - Read
  - Write
---

# Cursor Receipt Sync

Automatically find Cursor transactions in Ramp that are missing receipts, match them to Cursor invoices, download the invoice PDFs, and upload them to Ramp.

## Prerequisites

Update the path below to where you cloned the repo:
```
REPO_DIR="/path/to/cursor-ramp-receipts"
```

**Required credentials in `.env`:**

| Variable | Description | How to get |
|----------|-------------|------------|
| `RAMP_CLIENT_ID` | Ramp OAuth client ID | Ramp Dashboard > Company > Developer |
| `RAMP_CLIENT_SECRET` | Ramp OAuth client secret | Same as above |
| `RAMP_USER_ID` | Your Ramp user UUID | Query `load_users` via Ramp MCP |
| `CURSOR_SESSION_TOKEN` | WorkosCursorSessionToken cookie (optional -- browser fallback works without it) | HttpOnly cookie, not extractable via JS |
| `CURSOR_TEAM_ID` | Cursor team ID | Network tab on cursor.com dashboard |

## Workflow

### Step 0: Pre-flight Check

Always start by checking the Cursor token and finding missing receipts:

```bash
cd "$REPO_DIR"
source .venv/bin/activate && python sync_engine.py check-token 2>/dev/null
```

If the token is invalid, skip to **Step 1** (find-missing doesn't need the Cursor token), then use the **Browser Invoice Download** workflow to download PDFs directly from Stripe.

### Step 1: Find Missing Receipts in Ramp

```bash
cd "$REPO_DIR"
source .venv/bin/activate && python sync_engine.py find-missing --days 90 2>/dev/null
```

This outputs JSON with all Cursor transactions that have no receipts. Summarize the count, date range, and total amount for the user.

### Step 2: Match & Sync

**If the Cursor token is valid**, run the full automated sync:

```bash
cd "$REPO_DIR"
source .venv/bin/activate && python sync_engine.py sync --dry-run --days 90 2>/dev/null
```

Review the dry-run output. If matches look correct, run without `--dry-run` to upload.

**If the Cursor token is expired**, use the **Browser Invoice Download** workflow below to download each missing invoice PDF from Stripe and upload individually with `upload-receipt`.

## Browser Invoice Download (Primary when token expired)

The `WorkosCursorSessionToken` is **HttpOnly** and cannot be read via `document.cookie` or any JS API.
When the token is expired, skip token refresh entirely and download invoices via the Stripe billing portal:

1. **Open browser and navigate to Cursor billing:**
```
browser_tool open --foreground
browser_tool navigate https://cursor.com/dashboard/billing
```

2. **Wait for the page to load.** The browser tool may report "Security verification detected" on cursor.com pages even when there is no actual verification -- this is a false positive caused by the page having few accessibility elements during initial render. **Do not ask the user about verification.** Instead, wait and check:
```
browser_tool wait network-idle 8000
browser_tool snapshot
```
If the snapshot shows dashboard elements (links like "Billing & Invoices", "Overview", etc.), the page loaded fine. Only ask the user if the snapshot is truly empty after multiple retries.

3. **Click "Manage in Stripe" to open the Stripe billing portal:**
```
browser_tool click @<manage-in-stripe-ref>
browser_tool wait network-idle 8000
```

4. **Extract the Stripe invoice URL for the matching invoice.** Invoice links in the portal are `<a>` tags. Use evaluate to get the href:
```
browser_tool evaluate Array.from(document.querySelectorAll('a')).filter(a => a.textContent.includes('<amount>')).map(a => a.href).join(',')
```

5. **Navigate to the Stripe invoice page and download:**
```
browser_tool navigate <stripe_invoice_url>
browser_tool wait network-idle 8000
browser_tool click @<download-invoice-button>
browser_tool downloads wait 15000
```
The "Download invoice" button works reliably. "Download receipt" may trigger hCaptcha -- prefer "Download invoice".

6. **Copy the PDF and upload to Ramp:**
```bash
cp "<download_path>" "$REPO_DIR/receipts/<filename>.pdf"
cd "$REPO_DIR"
source .venv/bin/activate && python sync_engine.py upload-receipt \
  --transaction-id <ramp_tx_id> \
  --receipt-path receipts/<filename>.pdf
```

## Available Commands

| Command | Description |
|---------|-------------|
| `check-token` | Verify Cursor session token is valid |
| `update-token <token>` | Update the session token in .env |
| `find-missing --days N` | List Ramp Cursor transactions missing receipts |
| `list-invoices` | Fetch all Cursor invoices via API |
| `match --days N` | Match missing transactions to Cursor invoices |
| `sync --days N [--dry-run]` | Download PDFs and upload to Ramp |
| `upload-receipt --transaction-id X --receipt-path Y` | Upload a single receipt |

## Output Format

All commands output structured JSON to stdout. Logs go to stderr. Pipe stderr to `/dev/null` for clean JSON parsing.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Cursor token expired | Use Browser Invoice Download workflow above |
| Stripe PDF download fails | Use "Download invoice" not "Download receipt" (avoids hCaptcha) |
| Ramp upload 409 | Receipt already uploaded (idempotency key) -- safe to skip |
| No matches found | Check date range or look for amount mismatches (tax/refunds) |
| Ramp MCP timeout | `find-missing` uses Ramp REST API directly as fallback |

## Notes

- Receipts are saved to `./receipts/` before upload (cached for retry)
- Idempotency keys prevent duplicate uploads -- safe to re-run
- The Ramp MCP source is read-only; receipt uploads go through the Ramp REST API
- Cursor invoices use Stripe for billing, so PDFs come from Stripe's hosted invoice pages
- The session token expires ~6 months but can be invalidated on logout
