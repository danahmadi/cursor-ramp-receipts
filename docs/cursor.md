# Cursor Invoice → Ramp Receipt Sync

Automatically matches Cursor billing invoices with Ramp transactions and uploads them as receipts.

## Prerequisites

### 1. Cursor Web Session Token

The Cursor billing API requires a web session cookie (not an API key).

**How to get it:**

1. Go to [cursor.com/dashboard](https://cursor.com/dashboard) and log in
2. Open browser DevTools: **Cmd+Option+I** (Mac) or **F12** (Windows)
3. Go to **Application** tab → **Cookies** → **https://cursor.com**
4. Find `WorkosCursorSessionToken` and copy the **Value**

**Token Details:**
- Format: `user_XXXXX%3A%3AeyJhbGci...` (URL-encoded JWT)
- Expiration: ~6 months
- Scope: Web dashboard access only

### 2. Cursor Team ID

**How to get it:**

1. In the Cursor dashboard, click your **team name** in the left navbar
2. Open DevTools **Network** tab, look at any API request payload
3. The `teamId` is a numeric string (e.g., `1234567`)

## Environment Variables

Add these to your `.env` file:

```bash
CURSOR_SESSION_TOKEN=user_XXXXX%3A%3AeyJhbGci...
CURSOR_TEAM_ID=1234567
```

## Usage

```bash
# List all Cursor invoices
python cursor_ramp_sync.py list-invoices

# List Cursor transactions from Ramp (last 30 days)
python cursor_ramp_sync.py list-transactions --days 30

# Preview what would be synced (dry run)
python cursor_ramp_sync.py sync --dry-run --days 60

# Sync - download receipts and upload to Ramp
python cursor_ramp_sync.py sync --days 60
```

## How It Works

1. **Fetches Cursor invoices** from `cursor.com/api/dashboard/list-invoices`
2. **Fetches Ramp transactions** filtered by "Cursor" merchant
3. **Matches** by amount (exact) and date (within 3 days)
4. **Downloads receipts** from Stripe hosted invoice URLs
5. **Uploads to Ramp** via the receipts API

## Matching Logic

| Field | Match Criteria |
|-------|----------------|
| Amount | Exact match (Cursor cents ÷ 100 = Ramp dollars) |
| Date | Transaction within 3 days of invoice date |
| Merchant | Ramp merchant name contains "Cursor" |

## API Endpoints Used

### Cursor
- `POST https://cursor.com/api/dashboard/list-invoices`
  - Requires `WorkosCursorSessionToken` cookie
  - Requires `Origin: https://cursor.com` header

### Stripe (for receipts)
- Invoice page: `https://invoice.stripe.com/i/acct_XXX/live_XXX`
- Receipt PDF: `https://dashboard.stripe.com/receipts/invoices/XXX/pdf`

## Troubleshooting

### "Invalid origin for state-changing request"
Add the `Origin: https://cursor.com` header to Cursor API requests.

### Stripe PDF download fails
The automatic download sometimes fails due to JavaScript-heavy pages. The script will show the Stripe URL for manual download.

### Token expired
Re-fetch the `WorkosCursorSessionToken` from your browser cookies.

## Notes

- The session token expires in ~6 months but may be invalidated on logout
- Receipts are saved to `./receipts/` before upload
- Idempotency keys prevent duplicate uploads

