#!/usr/bin/env python3
"""
execute.py - Lidl Plus helper script

Commands:
  uv run execute.py get_token
  uv run execute.py download_receipts [MM.YYYY MM.YYYY]
"""

import json
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

TOKEN_FILE = Path("refresh_token")
CONFIG_FILE = Path("config.json")
OUTPUT_DIR = Path("receipts")


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def load_token():
    if not TOKEN_FILE.exists():
        print("No refresh token found. Run: uv run execute.py get_token")
        sys.exit(1)
    return TOKEN_FILE.read_text().strip()


def get_lidl_api():
    from lidlplus import LidlPlusApi

    config = load_config()
    language = config.get("language") or input("Language (de, en, ...): ")
    country = config.get("country") or input("Country (DE, AT, ...): ")

    if token := config.get("token"):
        return LidlPlusApi(language, country, token=token)

    return LidlPlusApi(language, country, refresh_token=load_token())


def cmd_get_token(debug=False):
    """Authenticate and save refresh token to file."""
    try:
        import getuseragent  # noqa: F401
        import oic  # noqa: F401
        import selenium  # noqa: F401
        import webdriver_manager  # noqa: F401
    except ImportError:
        print("Auth packages not installed. Run: uv sync --extra auth")
        sys.exit(1)

    from lidlplus import LidlPlusApi
    from lidlplus.exceptions import LegalTermsException, LoginError, WebBrowserException

    config = load_config()
    language = config.get("language") or input("Language (de, en, ...): ")
    country = config.get("country") or input("Country (DE, AT, ...): ")

    method = config.get("method") or input("Login with [e]mail or [p]hone: ").lower()
    if method not in ["e", "p"]:
        print("Invalid choice.")
        sys.exit(1)

    prompt = "Email: " if method == "e" else "Phone number: "
    stored = config.get("username")
    if stored:
        print(
            f"Using saved username: {stored} (leave blank to keep, or type a new one)"
        )
        override = input(prompt)
        username = override.strip() or stored
    else:
        username = input(prompt)
    password = getpass("Password: ")

    twofa_mode = config.get("twofa_mode")
    if not twofa_mode:
        twofa_choice = input("2FA via [p]hone or [e]mail (default: phone): ").lower()
        twofa_mode = "email" if twofa_choice == "e" else "phone"

    save_config(
        {
            "language": language,
            "country": country,
            "username": username,
            "method": method,
            "twofa_mode": twofa_mode,
        }
    )

    import os
    debug_log = "debug.log" if debug else None
    if debug:
        if os.path.exists("debug.log"):
            os.remove("debug.log")
        print("Debug mode: browser window will be visible. Network log -> debug.log")
    lidl = LidlPlusApi(language, country, debug_log=debug_log)
    try:
        lidl.login(
            username,
            password,
            method,
            verify_token_func=lambda: input(f"Enter 2FA code sent via {twofa_mode}: "),
            verify_mode=twofa_mode,
            headless=not debug,
        )
    except WebBrowserException as e:
        print(
            f"Browser error: {e}\nMake sure Chrome, Chromium or Firefox is installed."
        )
        sys.exit(1)
    except LoginError as e:
        print(f"Login failed: {e}")
        sys.exit(1)
    except LegalTermsException as e:
        print(f"Legal terms not accepted: {e}")
        sys.exit(1)

    TOKEN_FILE.write_text(lidl.refresh_token)
    print(f"\nToken saved to {TOKEN_FILE}")


def parse_month_arg(arg):
    """Parse MM.YYYY -> (year, month)."""
    try:
        month, year = arg.split(".")
        return int(year), int(month)
    except (ValueError, AttributeError):
        print(f"Invalid date format '{arg}' — expected MM.YYYY (e.g. 10.2025)")
        sys.exit(1)


def ticket_date_str(ticket):
    """Return the date string from a ticket dict, trying known field names."""
    # New API wraps data inside a 'ticket' key
    inner = ticket.get("ticket", ticket)
    for field in ("date", "dateTime", "Date", "DateTime", "purchaseDate"):
        if field in inner:
            return inner[field]
    return None


def ticket_filename(full, tid):
    """Return a filename stem: date-based if available, id as fallback."""
    date_str = ticket_date_str(full)
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d_%H-%M")
        except (ValueError, AttributeError):
            pass
    return tid


def in_range(date_str, from_ym, to_ym):
    """True if date_str (ISO format) falls within [from_ym, to_ym] inclusive."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return from_ym <= (dt.year, dt.month) <= to_ym
    except (ValueError, AttributeError):
        return False


_RECEIPT_CSS = """
    @page {
        size: 80mm auto;
        margin: 4mm;
    }
    body { margin: 0; }
    pre {
        font-family: monospace;
        font-size: 7.5pt;
        white-space: pre;
        line-height: 1.2;
    }
"""


def html_to_pdf(html_path, pdf_path):
    """Convert a receipt HTML file to a single-page PDF using WeasyPrint."""
    try:
        from weasyprint import HTML, CSS
    except ImportError:
        print("WeasyPrint not installed. Run: uv sync --extra pdf")
        sys.exit(1)
    HTML(filename=str(html_path)).write_pdf(
        str(pdf_path), stylesheets=[CSS(string=_RECEIPT_CSS)]
    )


def cmd_download_receipts(args):
    """Download receipts for a month range, default current month."""
    pdf = "--pdf" in args
    args = [a for a in args if a != "--pdf"]

    now = datetime.now()
    if len(args) == 0:
        from_ym = to_ym = (now.year, now.month)
    elif len(args) == 2:
        from_ym = parse_month_arg(args[0])
        to_ym = parse_month_arg(args[1])
        if from_ym > to_ym:
            print("Start date must not be after end date.")
            sys.exit(1)
    else:
        print("Usage: uv run execute.py download_receipts [--pdf] [MM.YYYY MM.YYYY]")
        sys.exit(1)

    if from_ym == to_ym:
        print(f"Fetching receipts for {from_ym[1]:02d}.{from_ym[0]}...")
    else:
        print(
            f"Fetching receipts from {from_ym[1]:02d}.{from_ym[0]} to {to_ym[1]:02d}.{to_ym[0]}..."
        )

    lidl = get_lidl_api()

    print("Loading ticket list...")
    in_range_tickets = lidl.tickets_in_range(from_ym, to_ym)
    print(f"Found {len(in_range_tickets)} receipt(s) in range.")

    count = len(in_range_tickets)
    if not count:
        print("Nothing to download.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    downloaded = []
    for i, t in enumerate(in_range_tickets, 1):
        tid = t["id"]
        print(f"[{i}/{count}] Downloading {tid}...")
        try:
            full = lidl.ticket(tid)
            downloaded.append(full)
            html = full.get("ticket", full).get("htmlPrintedReceipt", "")
            fname = ticket_filename(full, tid)
            html_path = OUTPUT_DIR / f"{fname}.html"
            html_path.write_text(html)
            if pdf and html:
                html_to_pdf(html_path, OUTPUT_DIR / f"{fname}.pdf")
        except Exception as e:
            print(f"  Failed: {e}")

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(downloaded, indent=2, ensure_ascii=False))
    print(f"\nDone. {len(downloaded)} receipt(s) saved to {OUTPUT_DIR}/")
    print(f"  HTML files : {OUTPUT_DIR}/<id>.html")
    print(f"  JSON summary: {summary_path}")


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]

    if not args:
        print("Usage:")
        print("  uv run execute.py [--debug] get_token")
        print("  uv run execute.py download_receipts [--pdf] [MM.YYYY MM.YYYY]")
        sys.exit(1)

    command = args[0]
    if command == "get_token":
        cmd_get_token(debug=debug)
    elif command == "download_receipts":
        cmd_download_receipts(args[1:])
    else:
        print(f"Unknown command: {command!r}")
        print("  get_token")
        print("  download_receipts [MM.YYYY MM.YYYY]")
        sys.exit(1)


if __name__ == "__main__":
    main()
