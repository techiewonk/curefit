"""
One-command auto flow for the Curefit scraper.

Runs the whole pipeline in a single session:
  1. Logs in  (sends OTP, asks you to type it at the terminal)
  2. Asks for the start date and end date
  3. Reads the centers to scrape from centers_to_process.csv
  4. Scrapes all reports -> SQLite + CSV reports + HTML dashboard

Just run:
    python run.py
"""
import datetime as dt
import re
import sys

from rich.prompt import Prompt

import curefit_login as C
import scrape_sales as S
from logutil import console, get_logger

log = get_logger()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ask_date(label, default):
    while True:
        val = Prompt.ask(f"[bold]{label}[/bold] (YYYY-MM-DD)", default=default).strip()
        if not DATE_RE.match(val):
            console.print("[red]Please use the format YYYY-MM-DD.[/red]")
            continue
        try:
            dt.date.fromisoformat(val)
            return val
        except ValueError:
            console.print("[red]That is not a valid date.[/red]")


def default_period():
    """First and last day of the previous month, as sensible defaults."""
    today = dt.date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), last_prev.isoformat()


def main():
    console.rule("[bold cyan]Curefit Scraper — auto flow[/bold cyan]")

    # 1. Login (interactive OTP)
    C.interactive_login()
    # bail out if session was not established
    _, extra = C.load_session()
    if not extra.get("erpToken"):
        console.print("[red]Login did not complete — aborting.[/red]")
        sys.exit(1)

    # 2. Ask period
    d_start, d_end = default_period()
    console.rule("[bold]Report period[/bold]")
    start = ask_date("Start date", d_start)
    end = ask_date("End date", d_end)
    if start > end:
        console.print("[yellow]Start is after end — swapping them.[/yellow]")
        start, end = end, start

    # 3 + 4. Scrape (centers come from centers_to_process.csv)
    console.rule("[bold]Scraping[/bold]")
    S.run_scrape(start, end)

    console.print("\n[bold green]All done.[/bold green] "
                  "Open the dashboard_*.html file to view results.")


if __name__ == "__main__":
    main()
