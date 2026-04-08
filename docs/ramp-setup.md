# Ramp API Setup

Common Ramp configuration used by all vendor sync scripts.

## Prerequisites

### 1. OAuth Client Credentials

**To obtain credentials:**

1. Go to [Ramp Dashboard](https://app.ramp.com) → **Company** → **Developer**
2. Create a new OAuth application or use existing credentials
3. You'll receive:
   - **Client ID**: `ramp_id_XXXXXXXXXXXXXXXXXXXXXXXXXXXXX`
   - **Client Secret**: `ramp_sec_XXXXXXXXXXXXXXXXXXXXXXXXXXXXX`

**Required Scopes:**
- `receipts:write` - Upload receipts
- `transactions:read` - Find transaction IDs

### 2. Your User ID

The Ramp user UUID is required for receipt uploads.

**Find it using the MCP server:**
```sql
-- First run: load_users
-- Then query:
SELECT id, first_name, last_name, email FROM users WHERE email LIKE '%your-email%';
```

**Or via API:**
```bash
curl -s "https://api.ramp.com/developer/v1/users/me" \
  -H "Authorization: Bearer $RAMP_ACCESS_TOKEN"
```

## Environment Variables

Add these to your `.env` file:

```bash
RAMP_CLIENT_ID=ramp_id_YOUR_CLIENT_ID
RAMP_CLIENT_SECRET=ramp_sec_YOUR_CLIENT_SECRET
RAMP_USER_ID=your-uuid-here
```

## OAuth Token Flow

The scripts handle token acquisition automatically:

```bash
curl -s -X POST 'https://api.ramp.com/developer/v1/token' \
  -u "$RAMP_CLIENT_ID:$RAMP_CLIENT_SECRET" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=client_credentials' \
  -d 'scope=receipts:write transactions:read'
```

**Response:**
```json
{
  "access_token": "ramp_business_tok_XXXXX",
  "expires_in": 864000,
  "token_type": "Bearer"
}
```

Tokens are valid for 10 days and cached in memory.

## API Endpoints

### Transactions
```
GET /developer/v1/transactions
```

Query params:
- `from_date` - ISO 8601 datetime
- `to_date` - ISO 8601 datetime
- `start` - Pagination cursor

### Receipts
```
POST /developer/v1/receipts
```

Form data:
- `user_id` - Your Ramp UUID
- `transaction_id` - Transaction UUID from API
- `idempotency_key` - Unique key to prevent duplicates
- `receipt` - PDF/image file

**Supported formats:** PDF, PNG, JPG, JPEG, WEBP, HEIF, HEIC

## Transaction URLs

To link to a transaction in the Ramp UI:

```
https://app.ramp.com/details/list/transactions/{transaction_id}
```

Where `{transaction_id}` is the UUID from the API.

## Common Issues

### "Access token not found"
The `ramp_sec_` key is a client secret, not an access token. Exchange it for a token using OAuth.

### "TransactionCanonical does not exist"
You're using the wrong transaction ID format. The spend export ID (from MCP) differs from the API transaction ID.

### "Missing data for required field: user_id"
The `user_id` must be a UUID from the Ramp API, not the `user_XXX` format from MCP.

## MCP Server (Optional)

For querying transactions interactively, add the Ramp MCP server:

```json
{
  "mcpServers": {
    "Ramp": {
      "command": "npx",
      "args": ["mcp-remote", "https://mcp.ramp.com/mcp"]
    }
  }
}
```

**Available Tools:**
- `load_spend_export` - Load transaction data
- `execute_query` - Run SQL queries
- `get_current_user` - Get authenticated user info
- `load_users` - Load team member data

Note: MCP is read-only for receipts—use API credentials for uploads.

