#!/usr/bin/env python3
"""
Cursor Invoice to Ramp Receipt Automation

Automatically matches Cursor billing invoices with Ramp card transactions
and uploads them as receipts.
"""

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import click
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()

console = Console()

# Directory for downloaded receipts
RECEIPTS_DIR = Path(__file__).parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


class CursorAPI:
    """Client for Cursor's billing API."""

    BASE_URL = "https://cursor.com/api/dashboard"

    def __init__(self, session_token: str, team_id: str):
        self.session_token = session_token
        self.team_id = team_id
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Origin": "https://cursor.com",
        })
        self.session.cookies.set("WorkosCursorSessionToken", session_token)

    def list_invoices(self, page: int = 1, page_size: int = 100) -> dict:
        """Fetch invoices from Cursor."""
        response = self.session.post(
            f"{self.BASE_URL}/list-invoices",
            json={"teamId": int(self.team_id), "page": page, "pageSize": page_size},
        )
        response.raise_for_status()
        return response.json()

    def get_all_invoices(self) -> list[dict]:
        """Fetch all invoices, handling pagination."""
        all_invoices = []
        page = 1

        while True:
            data = self.list_invoices(page=page)
            invoices = data.get("invoices", [])
            if not invoices:
                break
            all_invoices.extend(invoices)
            if len(invoices) < 100:
                break
            page += 1

        return all_invoices


class RampAPI:
    """Client for Ramp's Developer API."""

    BASE_URL = "https://api.ramp.com/developer/v1"
    TOKEN_URL = "https://api.ramp.com/developer/v1/token"

    def __init__(self, client_id: str, client_secret: str, user_id: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

    def _get_access_token(self) -> str:
        """Get or refresh OAuth access token."""
        if self._access_token and self._token_expires and datetime.now() < self._token_expires:
            return self._access_token

        response = requests.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "scope": "receipts:write transactions:read",
            },
        )
        response.raise_for_status()
        data = response.json()

        self._access_token = data["access_token"]
        # Expire 5 minutes early to be safe
        self._token_expires = datetime.now() + timedelta(seconds=data["expires_in"] - 300)

        return self._access_token

    def _headers(self) -> dict:
        """Get authorization headers."""
        return {"Authorization": f"Bearer {self._get_access_token()}"}

    def get_transactions(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        merchant_name: Optional[str] = None,
    ) -> list[dict]:
        """Fetch transactions from Ramp."""
        from urllib.parse import urlparse, parse_qs

        params = {}
        if from_date:
            params["from_date"] = from_date.strftime("%Y-%m-%dT00:00:00Z")
        if to_date:
            params["to_date"] = to_date.strftime("%Y-%m-%dT23:59:59Z")

        all_transactions = []

        while True:
            response = requests.get(
                f"{self.BASE_URL}/transactions",
                headers=self._headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            transactions = data.get("data", [])
            all_transactions.extend(transactions)

            # Check for pagination
            next_value = data.get("page", {}).get("next")
            if not next_value:
                break

            # next_value can be a URL or a cursor - extract the start param if it's a URL
            if next_value.startswith("http"):
                parsed = urlparse(next_value)
                query_params = parse_qs(parsed.query)
                start_values = query_params.get("start", [])
                if start_values:
                    params["start"] = start_values[0]
                else:
                    break
            else:
                # It's just a cursor token
                params["start"] = next_value

        # Filter by merchant if specified
        if merchant_name:
            all_transactions = [
                t for t in all_transactions
                if merchant_name.lower() in t.get("merchant_name", "").lower()
            ]

        return all_transactions

    def get_transaction(self, transaction_id: str) -> dict:
        """Get a single transaction by ID."""
        response = requests.get(
            f"{self.BASE_URL}/transactions/{transaction_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def upload_receipt(
        self,
        transaction_id: str,
        receipt_path: Path,
        idempotency_key: str,
    ) -> dict:
        """Upload a receipt to Ramp."""
        with open(receipt_path, "rb") as f:
            response = requests.post(
                f"{self.BASE_URL}/receipts",
                headers=self._headers(),
                files={"receipt": f},
                data={
                    "user_id": self.user_id,
                    "transaction_id": transaction_id,
                    "idempotency_key": idempotency_key,
                },
            )
        response.raise_for_status()
        return response.json()


class StripeReceiptDownloader:
    """Downloads receipts from Stripe invoice pages."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })

    def download_receipt(self, hosted_invoice_url: str, save_path: Path) -> bool:
        """
        Download receipt PDF from Stripe hosted invoice.

        Note: This extracts the receipt PDF URL from the invoice page.
        The receipt URL format is typically:
        https://invoicedata.stripe.com/receipt_pdf_file_url/acct_XXX/live_XXX
        """
        try:
            # Get the invoice page
            response = self.session.get(hosted_invoice_url)
            response.raise_for_status()

            # Look for the receipt PDF URL in the page
            # The URL is typically in a script tag or data attribute
            content = response.text

            # Try to find the receipt PDF URL pattern
            receipt_url_match = re.search(
                r'https://invoicedata\.stripe\.com/receipt_pdf_file_url/[^"\']+',
                content
            )

            if receipt_url_match:
                receipt_url = receipt_url_match.group(0)
                # Get the actual PDF URL (this returns a signed S3 URL)
                pdf_response = self.session.get(receipt_url)
                pdf_response.raise_for_status()

                # The response might be a redirect or direct PDF
                if pdf_response.headers.get("Content-Type", "").startswith("application/pdf"):
                    save_path.write_bytes(pdf_response.content)
                    return True

                # If it's JSON with a URL
                try:
                    pdf_data = pdf_response.json()
                    if "url" in pdf_data:
                        final_response = self.session.get(pdf_data["url"])
                        final_response.raise_for_status()
                        save_path.write_bytes(final_response.content)
                        return True
                except ValueError:
                    pass

            # Fallback: Look for download button/link patterns
            # Try the invoice PDF as fallback (not ideal, but better than nothing)
            invoice_pdf_match = re.search(
                r'https://[^"\']*invoice[^"\']*\.pdf[^"\']*',
                content,
                re.IGNORECASE
            )

            if invoice_pdf_match:
                pdf_url = invoice_pdf_match.group(0)
                pdf_response = self.session.get(pdf_url)
                pdf_response.raise_for_status()
                save_path.write_bytes(pdf_response.content)
                return True

            return False

        except requests.RequestException as e:
            console.print(f"[red]Error downloading receipt: {e}[/red]")
            return False


def match_transactions_to_invoices(
    transactions: list[dict],
    invoices: list[dict],
) -> list[tuple[dict, dict]]:
    """
    Match Ramp transactions to Cursor invoices.

    Matching criteria:
    - Amount must match exactly (Cursor uses cents, Ramp uses dollars)
    - Transaction date should be within 3 days of invoice date
    """
    matches = []

    for transaction in transactions:
        ramp_amount = transaction.get("amount", 0)
        ramp_date_str = transaction.get("user_transaction_time", "")

        if not ramp_date_str:
            continue

        # Parse Ramp date
        try:
            ramp_date = datetime.fromisoformat(ramp_date_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        for invoice in invoices:
            # Convert cents to dollars for comparison
            cursor_amount = invoice.get("amountCents", 0) / 100

            # Check amount match (with small tolerance for floating point)
            if abs(ramp_amount - cursor_amount) > 0.01:
                continue

            # Parse Cursor date (timestamp in milliseconds)
            cursor_timestamp = invoice.get("date")
            if not cursor_timestamp:
                continue

            try:
                cursor_date = datetime.fromtimestamp(int(cursor_timestamp) / 1000)
            except (ValueError, OSError):
                continue

            # Check date proximity (within 3 days)
            date_diff = abs((ramp_date.replace(tzinfo=None) - cursor_date).days)
            if date_diff <= 3:
                matches.append((transaction, invoice))
                break  # Move to next transaction

    return matches


def format_amount(cents: int) -> str:
    """Format cents as dollar amount."""
    return f"${cents / 100:.2f}"


def format_date(timestamp_ms: str) -> str:
    """Format millisecond timestamp as date string."""
    try:
        dt = datetime.fromtimestamp(int(timestamp_ms) / 1000)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return "Unknown"


@click.group()
def cli():
    """Cursor Invoice to Ramp Receipt Automation."""
    pass


@cli.command()
def list_invoices():
    """List Cursor invoices."""
    session_token = os.getenv("CURSOR_SESSION_TOKEN")
    team_id = os.getenv("CURSOR_TEAM_ID")

    if not session_token or not team_id:
        console.print("[red]Missing CURSOR_SESSION_TOKEN or CURSOR_TEAM_ID[/red]")
        raise click.Abort()

    cursor = CursorAPI(session_token, team_id)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Fetching Cursor invoices...", total=None)
        invoices = cursor.get_all_invoices()

    table = Table(title="Cursor Invoices")
    table.add_column("Date", style="cyan")
    table.add_column("Amount", style="green", justify="right")
    table.add_column("Status", style="yellow")
    table.add_column("Description")
    table.add_column("Invoice ID", style="dim")

    for invoice in invoices:
        table.add_row(
            format_date(invoice.get("date", "")),
            format_amount(invoice.get("amountCents", 0)),
            invoice.get("status", ""),
            invoice.get("description", "")[:50],
            invoice.get("invoiceId", ""),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(invoices)} invoices[/dim]")


@cli.command()
@click.option("--days", default=30, help="Number of days to look back")
def list_transactions(days: int):
    """List Cursor transactions from Ramp."""
    client_id = os.getenv("RAMP_CLIENT_ID")
    client_secret = os.getenv("RAMP_CLIENT_SECRET")
    user_id = os.getenv("RAMP_USER_ID")

    if not all([client_id, client_secret, user_id]):
        console.print("[red]Missing Ramp credentials (RAMP_CLIENT_ID, RAMP_CLIENT_SECRET, RAMP_USER_ID)[/red]")
        raise click.Abort()

    ramp = RampAPI(client_id, client_secret, user_id)

    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Fetching Ramp transactions...", total=None)
        transactions = ramp.get_transactions(from_date, to_date, merchant_name="cursor")

    table = Table(title=f"Cursor Transactions (last {days} days)")
    table.add_column("Date", style="cyan")
    table.add_column("Amount", style="green", justify="right")
    table.add_column("Merchant", style="yellow")
    table.add_column("Has Receipt", style="magenta")
    table.add_column("Transaction ID", style="dim")

    for tx in transactions:
        date_str = tx.get("user_transaction_time", "")[:10]
        has_receipt = "✓" if tx.get("receipts") else "✗"

        table.add_row(
            date_str,
            f"${tx.get('amount', 0):.2f}",
            tx.get("merchant_name", ""),
            has_receipt,
            tx.get("id", ""),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(transactions)} transactions[/dim]")


@cli.command()
@click.option("--days", default=60, help="Number of days to look back")
@click.option("--dry-run", is_flag=True, help="Show matches without uploading")
def sync(days: int, dry_run: bool):
    """Sync Cursor invoices to Ramp as receipts."""
    # Load credentials
    cursor_token = os.getenv("CURSOR_SESSION_TOKEN")
    cursor_team_id = os.getenv("CURSOR_TEAM_ID")
    ramp_client_id = os.getenv("RAMP_CLIENT_ID")
    ramp_client_secret = os.getenv("RAMP_CLIENT_SECRET")
    ramp_user_id = os.getenv("RAMP_USER_ID")

    missing = []
    if not cursor_token:
        missing.append("CURSOR_SESSION_TOKEN")
    if not cursor_team_id:
        missing.append("CURSOR_TEAM_ID")
    if not ramp_client_id:
        missing.append("RAMP_CLIENT_ID")
    if not ramp_client_secret:
        missing.append("RAMP_CLIENT_SECRET")
    if not ramp_user_id:
        missing.append("RAMP_USER_ID")

    if missing:
        console.print(f"[red]Missing credentials: {', '.join(missing)}[/red]")
        raise click.Abort()

    # Initialize clients
    cursor = CursorAPI(cursor_token, cursor_team_id)
    ramp = RampAPI(ramp_client_id, ramp_client_secret, ramp_user_id)
    stripe_downloader = StripeReceiptDownloader()

    console.print("\n[bold]🔄 Cursor → Ramp Receipt Sync[/bold]\n")

    # Step 1: Fetch Cursor invoices
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching Cursor invoices...", total=None)
        invoices = cursor.get_all_invoices()
        progress.update(task, description=f"Found {len(invoices)} Cursor invoices")
        time.sleep(0.5)

    # Step 2: Fetch Ramp transactions
    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching Ramp transactions...", total=None)
        transactions = ramp.get_transactions(from_date, to_date, merchant_name="cursor")
        progress.update(task, description=f"Found {len(transactions)} Cursor transactions")
        time.sleep(0.5)

    # Filter to transactions without receipts
    missing_receipts = [t for t in transactions if not t.get("receipts")]
    console.print(f"[yellow]📋 {len(missing_receipts)} transactions missing receipts[/yellow]\n")

    if not missing_receipts:
        console.print("[green]✅ All Cursor transactions have receipts![/green]")
        return

    # Step 3: Match transactions to invoices
    matches = match_transactions_to_invoices(missing_receipts, invoices)

    if not matches:
        console.print("[yellow]⚠️  No matching invoices found for transactions missing receipts[/yellow]")
        return

    # Display matches
    table = Table(title="Matched Transactions → Invoices")
    table.add_column("Ramp Date", style="cyan")
    table.add_column("Amount", style="green", justify="right")
    table.add_column("Cursor Date", style="cyan")
    table.add_column("Invoice ID", style="dim")

    for tx, inv in matches:
        table.add_row(
            tx.get("user_transaction_time", "")[:10],
            f"${tx.get('amount', 0):.2f}",
            format_date(inv.get("date", "")),
            inv.get("invoiceId", "")[:20] + "...",
        )

    console.print(table)

    if dry_run:
        console.print("\n[yellow]🔍 Dry run - no receipts uploaded[/yellow]")
        return

    # Step 4: Download and upload receipts
    console.print(f"\n[bold]📤 Uploading {len(matches)} receipts...[/bold]\n")

    success_count = 0
    for tx, inv in matches:
        invoice_id = inv.get("invoiceId", "")
        amount = tx.get("amount", 0)
        tx_id = tx.get("id", "")
        hosted_url = inv.get("hostedInvoiceUrl", "")

        if not hosted_url:
            console.print(f"[red]✗ No hosted URL for invoice {invoice_id}[/red]")
            continue

        # Download receipt
        receipt_filename = f"cursor-receipt-{invoice_id[:20]}.pdf"
        receipt_path = RECEIPTS_DIR / receipt_filename

        console.print(f"  Downloading receipt for ${amount:.2f}...", end=" ")

        if stripe_downloader.download_receipt(hosted_url, receipt_path):
            console.print("[green]✓[/green]", end=" ")

            # Upload to Ramp
            idempotency_key = f"cursor-{invoice_id}-{int(amount * 100)}"
            try:
                ramp.upload_receipt(tx_id, receipt_path, idempotency_key)
                console.print("[green]uploaded![/green]")
                success_count += 1
            except requests.HTTPError as e:
                console.print(f"[red]upload failed: {e}[/red]")
        else:
            console.print("[yellow]download failed (manual download required)[/yellow]")
            console.print(f"    [dim]URL: {hosted_url}[/dim]")

    console.print(f"\n[bold green]✅ Successfully uploaded {success_count}/{len(matches)} receipts[/bold green]")


@cli.command()
@click.argument("invoice_url")
@click.option("--output", "-o", type=click.Path(), help="Output path for PDF")
def download_receipt(invoice_url: str, output: Optional[str]):
    """Download a receipt from a Stripe invoice URL."""
    downloader = StripeReceiptDownloader()

    if output:
        save_path = Path(output)
    else:
        save_path = RECEIPTS_DIR / f"receipt-{int(time.time())}.pdf"

    console.print(f"Downloading receipt from {invoice_url}...")

    if downloader.download_receipt(invoice_url, save_path):
        console.print(f"[green]✓ Saved to {save_path}[/green]")
    else:
        console.print("[red]✗ Failed to download receipt[/red]")
        console.print("[yellow]Try downloading manually from the Stripe invoice page[/yellow]")


@cli.command()
def check_config():
    """Check if all required credentials are configured."""
    checks = [
        ("CURSOR_SESSION_TOKEN", os.getenv("CURSOR_SESSION_TOKEN")),
        ("CURSOR_TEAM_ID", os.getenv("CURSOR_TEAM_ID")),
        ("RAMP_CLIENT_ID", os.getenv("RAMP_CLIENT_ID")),
        ("RAMP_CLIENT_SECRET", os.getenv("RAMP_CLIENT_SECRET")),
        ("RAMP_USER_ID", os.getenv("RAMP_USER_ID")),
    ]

    table = Table(title="Configuration Check")
    table.add_column("Variable", style="cyan")
    table.add_column("Status")
    table.add_column("Preview", style="dim")

    all_good = True
    for name, value in checks:
        if value:
            preview = value[:20] + "..." if len(value) > 20 else value
            table.add_row(name, "[green]✓ Set[/green]", preview)
        else:
            table.add_row(name, "[red]✗ Missing[/red]", "")
            all_good = False

    console.print(table)

    if all_good:
        console.print("\n[green]✅ All credentials configured![/green]")
    else:
        console.print("\n[yellow]⚠️  Some credentials are missing. See .env.example for required values.[/yellow]")


if __name__ == "__main__":
    cli()

