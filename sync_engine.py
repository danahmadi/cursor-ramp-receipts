#!/usr/bin/env python3
"""
Cursor Receipt Sync Engine

Agent-friendly CLI for syncing Cursor invoices to Ramp as receipts.
All commands output structured JSON to stdout; logs go to stderr.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import click
import requests
from dotenv import load_dotenv

load_dotenv()

RECEIPTS_DIR = Path(__file__).parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


def log(msg: str):
    """Log to stderr so stdout stays clean for JSON output."""
    print(msg, file=sys.stderr)


def output(data):
    """Write structured JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Ramp API Client
# ---------------------------------------------------------------------------

class RampAPI:
    BASE_URL = "https://api.ramp.com/developer/v1"

    def __init__(self):
        self.client_id = os.getenv("RAMP_CLIENT_ID")
        self.client_secret = os.getenv("RAMP_CLIENT_SECRET")
        self.user_id = os.getenv("RAMP_USER_ID")
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

        if not all([self.client_id, self.client_secret, self.user_id]):
            raise click.ClickException(
                "Missing RAMP_CLIENT_ID, RAMP_CLIENT_SECRET, or RAMP_USER_ID in .env"
            )

    def _get_token(self) -> str:
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        resp = requests.post(
            f"{self.BASE_URL}/token",
            auth=(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "receipts:write transactions:read"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = datetime.now() + timedelta(seconds=data["expires_in"] - 300)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def get_transactions(self, from_date: datetime, to_date: datetime) -> list[dict]:
        params = {
            "from_date": from_date.strftime("%Y-%m-%dT00:00:00Z"),
            "to_date": to_date.strftime("%Y-%m-%dT23:59:59Z"),
        }
        all_txns = []
        while True:
            resp = requests.get(f"{self.BASE_URL}/transactions", headers=self._headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
            all_txns.extend(data.get("data", []))
            next_val = data.get("page", {}).get("next")
            if not next_val:
                break
            if next_val.startswith("http"):
                parsed = urlparse(next_val)
                qp = parse_qs(parsed.query)
                if "start" in qp:
                    params["start"] = qp["start"][0]
                else:
                    break
            else:
                params["start"] = next_val
        return all_txns

    def get_cursor_transactions(self, from_date: datetime, to_date: datetime) -> list[dict]:
        txns = self.get_transactions(from_date, to_date)
        return [t for t in txns if "cursor" in t.get("merchant_name", "").lower()]

    def upload_receipt(self, transaction_id: str, receipt_path: Path, idempotency_key: str) -> dict:
        with open(receipt_path, "rb") as f:
            resp = requests.post(
                f"{self.BASE_URL}/receipts",
                headers=self._headers(),
                files={"receipt": f},
                data={
                    "user_id": self.user_id,
                    "transaction_id": transaction_id,
                    "idempotency_key": idempotency_key,
                },
            )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Cursor API Client
# ---------------------------------------------------------------------------

class CursorAPI:
    BASE_URL = "https://cursor.com/api/dashboard"

    def __init__(self):
        self.session_token = os.getenv("CURSOR_SESSION_TOKEN")
        self.team_id = os.getenv("CURSOR_TEAM_ID")

        if not self.session_token or not self.team_id:
            raise click.ClickException("Missing CURSOR_SESSION_TOKEN or CURSOR_TEAM_ID in .env")

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Origin": "https://cursor.com",
        })
        self.session.cookies.set("WorkosCursorSessionToken", self.session_token)

    def get_invoices(self) -> list[dict]:
        all_invoices = []
        page = 1
        while True:
            resp = self.session.post(
                f"{self.BASE_URL}/list-invoices",
                json={"teamId": int(self.team_id), "page": page, "pageSize": 100},
            )
            if resp.status_code in (301, 302, 303, 307, 308, 401, 403, 404):
                raise click.ClickException(
                    "Cursor session token is expired or invalid. "
                    "Refresh it:\n"
                    "  1. Open cursor.com/dashboard in a browser and log in\n"
                    "  2. DevTools > Application > Cookies > WorkosCursorSessionToken\n"
                    "  3. Update CURSOR_SESSION_TOKEN in .env\n\n"
                    "Or use the browser-based workflow via Craft Agent."
                )
            resp.raise_for_status()
            invoices = resp.json().get("invoices", [])
            if not invoices:
                break
            all_invoices.extend(invoices)
            if len(invoices) < 100:
                break
            page += 1
        return all_invoices


# ---------------------------------------------------------------------------
# Stripe Receipt Downloader
# ---------------------------------------------------------------------------

class StripeDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })

    @staticmethod
    def _receipt_data_url(hosted_url: str) -> Optional[str]:
        """Build the invoicedata receipt URL from a Stripe hosted invoice URL.

        hosted_url looks like:
          https://invoice.stripe.com/i/acct_XXX/live_YYY?s=ap
        We transform it to:
          https://invoicedata.stripe.com/invoice_receipt_file_url/acct_XXX/live_YYY
        """
        parsed = urlparse(hosted_url)
        if "stripe.com" not in parsed.hostname:
            return None
        # Path is /i/acct_XXX/live_YYY — strip the /i prefix
        path = parsed.path
        if path.startswith("/i/"):
            path = path[3:]  # "acct_XXX/live_YYY"
        elif path.startswith("/i"):
            path = path[2:]
        else:
            return None
        return f"https://invoicedata.stripe.com/invoice_receipt_file_url/{path}"

    def download(self, hosted_url: str, save_path: Path) -> bool:
        try:
            # Step 1: Build the receipt data URL from the hosted invoice URL
            receipt_data_url = self._receipt_data_url(hosted_url)
            if receipt_data_url:
                resp = self.session.get(receipt_data_url)
                if resp.ok:
                    # Step 2: Response is JSON with a file_url pointing to the PDF
                    try:
                        data = resp.json()
                        file_url = data.get("file_url")
                        if file_url:
                            pdf_resp = self.session.get(file_url)
                            pdf_resp.raise_for_status()
                            save_path.write_bytes(pdf_resp.content)
                            return True
                    except (ValueError, KeyError):
                        pass

            # Fallback: fetch the hosted page and look for PDF links
            resp = self.session.get(hosted_url)
            resp.raise_for_status()
            content = resp.text

            inv_match = re.search(
                r'https://[^"\']*invoice[^"\']*\.pdf[^"\']*', content, re.IGNORECASE
            )
            if inv_match:
                pdf_resp = self.session.get(inv_match.group(0))
                pdf_resp.raise_for_status()
                save_path.write_bytes(pdf_resp.content)
                return True

            return False
        except requests.RequestException as e:
            log(f"Download error: {e}")
            return False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_transactions_to_invoices(
    transactions: list[dict], invoices: list[dict]
) -> list[dict]:
    matches = []
    used_invoices = set()

    for tx in transactions:
        ramp_amount = tx.get("amount", 0)
        ramp_date_str = tx.get("user_transaction_time", "")
        if not ramp_date_str:
            continue
        try:
            ramp_date = datetime.fromisoformat(ramp_date_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        for inv in invoices:
            inv_id = inv.get("invoiceId", "")
            if inv_id in used_invoices:
                continue

            cursor_amount = inv.get("amountCents", 0) / 100
            if abs(ramp_amount - cursor_amount) > 0.01:
                continue

            cursor_ts = inv.get("date")
            if not cursor_ts:
                continue
            try:
                cursor_date = datetime.fromtimestamp(int(cursor_ts) / 1000)
            except (ValueError, OSError):
                continue

            if abs((ramp_date.replace(tzinfo=None) - cursor_date).days) <= 3:
                matches.append({
                    "transaction_id": tx.get("id"),
                    "ramp_date": ramp_date_str[:10],
                    "ramp_amount": ramp_amount,
                    "merchant": tx.get("merchant_name", ""),
                    "invoice_id": inv_id,
                    "cursor_date": cursor_date.strftime("%Y-%m-%d"),
                    "cursor_amount": cursor_amount,
                    "hosted_invoice_url": inv.get("hostedInvoiceUrl", ""),
                    "description": inv.get("description", ""),
                })
                used_invoices.add(inv_id)
                break

    return matches


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Cursor Receipt Sync Engine — structured JSON output for agent use."""
    pass


@cli.command("find-missing")
@click.option("--days", default=90, help="Days to look back")
def find_missing(days: int):
    """Find Cursor transactions in Ramp that are missing receipts."""
    ramp = RampAPI()
    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()

    log(f"Fetching Cursor transactions from Ramp (last {days} days)...")
    txns = ramp.get_cursor_transactions(from_date, to_date)

    missing = []
    has_receipt = []
    for t in txns:
        entry = {
            "transaction_id": t.get("id"),
            "date": t.get("user_transaction_time", "")[:10],
            "amount": t.get("amount", 0),
            "merchant": t.get("merchant_name", ""),
            "receipt_count": len(t.get("receipts", [])),
        }
        if not t.get("receipts"):
            missing.append(entry)
        else:
            has_receipt.append(entry)

    output({
        "total_cursor_transactions": len(txns),
        "missing_receipts": len(missing),
        "has_receipts": len(has_receipt),
        "total_missing_amount": round(sum(m["amount"] for m in missing), 2),
        "transactions_missing_receipts": missing,
    })


@cli.command("list-invoices")
def list_invoices():
    """Fetch all invoices from Cursor billing API."""
    cursor = CursorAPI()

    log("Fetching Cursor invoices...")
    invoices = cursor.get_invoices()

    result = []
    for inv in invoices:
        ts = inv.get("date")
        try:
            date_str = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d") if ts else ""
        except (ValueError, OSError):
            date_str = ""

        result.append({
            "invoice_id": inv.get("invoiceId", ""),
            "date": date_str,
            "amount": inv.get("amountCents", 0) / 100,
            "status": inv.get("status", ""),
            "description": inv.get("description", ""),
            "hosted_invoice_url": inv.get("hostedInvoiceUrl", ""),
        })

    output({"total_invoices": len(result), "invoices": result})


@cli.command("match")
@click.option("--days", default=90, help="Days to look back")
def match(days: int):
    """Match Ramp transactions missing receipts to Cursor invoices."""
    ramp = RampAPI()
    cursor = CursorAPI()

    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()

    log("Fetching Ramp transactions...")
    txns = ramp.get_cursor_transactions(from_date, to_date)
    missing = [t for t in txns if not t.get("receipts")]

    log(f"Found {len(missing)} transactions missing receipts")

    log("Fetching Cursor invoices...")
    invoices = cursor.get_invoices()

    log("Matching...")
    matches = match_transactions_to_invoices(missing, invoices)

    unmatched_txns = []
    matched_tx_ids = {m["transaction_id"] for m in matches}
    for t in missing:
        if t["id"] not in matched_tx_ids:
            unmatched_txns.append({
                "transaction_id": t["id"],
                "date": t.get("user_transaction_time", "")[:10],
                "amount": t.get("amount", 0),
            })

    output({
        "missing_receipt_count": len(missing),
        "matched_count": len(matches),
        "unmatched_count": len(unmatched_txns),
        "matches": matches,
        "unmatched_transactions": unmatched_txns,
    })


@cli.command("sync")
@click.option("--days", default=90, help="Days to look back")
@click.option("--dry-run", is_flag=True, help="Preview without uploading")
def sync(days: int, dry_run: bool):
    """Download invoice PDFs and upload as receipts to Ramp."""
    ramp = RampAPI()
    cursor = CursorAPI()
    stripe = StripeDownloader()

    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()

    log("Fetching Ramp transactions...")
    txns = ramp.get_cursor_transactions(from_date, to_date)
    missing = [t for t in txns if not t.get("receipts")]

    log(f"{len(missing)} transactions missing receipts")

    log("Fetching Cursor invoices...")
    invoices = cursor.get_invoices()

    log("Matching...")
    matches = match_transactions_to_invoices(missing, invoices)
    log(f"{len(matches)} matches found")

    if dry_run:
        output({
            "mode": "dry_run",
            "matches": matches,
            "would_upload": len(matches),
        })
        return

    results = []
    for m in matches:
        tx_id = m["transaction_id"]
        inv_id = m["invoice_id"]
        hosted_url = m["hosted_invoice_url"]
        amount = m["ramp_amount"]

        result = {
            "transaction_id": tx_id,
            "invoice_id": inv_id,
            "amount": amount,
            "date": m["ramp_date"],
        }

        if not hosted_url:
            result["status"] = "skipped"
            result["reason"] = "no_hosted_url"
            results.append(result)
            continue

        # Download PDF
        filename = f"cursor-{inv_id[:20]}.pdf"
        receipt_path = RECEIPTS_DIR / filename
        log(f"Downloading receipt for ${amount:.2f} ({m['ramp_date']})...")

        if stripe.download(hosted_url, receipt_path):
            result["pdf_path"] = str(receipt_path)

            # Upload to Ramp
            idempotency_key = f"cursor-{inv_id}-{int(amount * 100)}"
            try:
                ramp.upload_receipt(tx_id, receipt_path, idempotency_key)
                result["status"] = "uploaded"
                log(f"  Uploaded successfully")
            except requests.HTTPError as e:
                result["status"] = "upload_failed"
                result["error"] = str(e)
                log(f"  Upload failed: {e}")
        else:
            result["status"] = "download_failed"
            result["hosted_url"] = hosted_url
            log(f"  PDF download failed — use browser fallback")

        results.append(result)

    uploaded = sum(1 for r in results if r["status"] == "uploaded")
    failed = sum(1 for r in results if r["status"] in ("download_failed", "upload_failed"))

    output({
        "mode": "sync",
        "total_matched": len(matches),
        "uploaded": uploaded,
        "failed": failed,
        "results": results,
    })


@cli.command("upload-receipt")
@click.option("--transaction-id", required=True, help="Ramp transaction UUID")
@click.option("--receipt-path", required=True, type=click.Path(exists=True), help="Path to receipt PDF")
def upload_receipt(transaction_id: str, receipt_path: str):
    """Upload a single receipt file to a Ramp transaction."""
    ramp = RampAPI()
    path = Path(receipt_path)

    idempotency_key = f"manual-{transaction_id}-{int(time.time())}"
    log(f"Uploading {path.name} to transaction {transaction_id[:12]}...")

    try:
        result = ramp.upload_receipt(transaction_id, path, idempotency_key)
        output({"status": "uploaded", "transaction_id": transaction_id, "receipt": path.name, "response": result})
    except requests.HTTPError as e:
        output({"status": "failed", "transaction_id": transaction_id, "error": str(e)})
        sys.exit(1)


@cli.command("check-token")
def check_token():
    """Check if the Cursor session token is valid."""
    try:
        cursor = CursorAPI()
    except click.ClickException as e:
        output({"valid": False, "error": str(e)})
        sys.exit(1)

    try:
        resp = cursor.session.post(
            f"{cursor.BASE_URL}/list-invoices",
            json={"teamId": int(cursor.team_id), "page": 1, "pageSize": 1},
        )
        if resp.status_code in (301, 302, 303, 307, 308, 401, 403, 404):
            output({
                "valid": False,
                "error": "Token expired or invalid",
                "action": "Refresh CURSOR_SESSION_TOKEN from browser cookies",
            })
            sys.exit(1)
        resp.raise_for_status()
        invoices = resp.json().get("invoices", [])
        output({
            "valid": True,
            "sample_invoice_count": len(invoices),
        })
    except Exception as e:
        output({"valid": False, "error": str(e)})
        sys.exit(1)


@cli.command("update-token")
@click.argument("token")
def update_token(token: str):
    """Update the Cursor session token in .env."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        output({"status": "error", "error": ".env file not found"})
        sys.exit(1)

    content = env_path.read_text()
    import re as re_mod
    if re_mod.search(r'^CURSOR_SESSION_TOKEN=.*$', content, re_mod.MULTILINE):
        content = re_mod.sub(
            r'^CURSOR_SESSION_TOKEN=.*$',
            f'CURSOR_SESSION_TOKEN={token}',
            content,
            flags=re_mod.MULTILINE,
        )
    else:
        content += f'\nCURSOR_SESSION_TOKEN={token}\n'

    env_path.write_text(content)
    output({"status": "updated", "token_prefix": token[:30] + "..."})


if __name__ == "__main__":
    cli()
