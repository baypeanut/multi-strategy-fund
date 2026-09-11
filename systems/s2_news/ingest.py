"""Ingestion — SEC EDGAR filings (free, no API key).

EDGAR exposes a JSON submissions API. We map tickers -> CIK, then pull recent
filings (8-K, 10-Q, etc.). SEC requires a descriptive User-Agent. This is the
backtestable, point-in-time event source; live headline feeds (GDELT/RSS) plug
into the same NewsItem interface.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime
from functools import lru_cache

import certifi

from .types import FilingItem

_UA = {"User-Agent": "Trading Research Agent research@example.com"}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


@lru_cache(maxsize=1)
def ticker_to_cik() -> dict[str, str]:
    data = json.loads(_get(_TICKERS_URL))
    out: dict[str, str] = {}
    for row in data.values():
        out[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return out


class EDGARClient:
    def recent_filings(
        self, ticker: str, forms: tuple[str, ...] = ("8-K",), limit: int = 20
    ) -> list[FilingItem]:
        cik = ticker_to_cik().get(ticker.upper())
        if not cik:
            return []
        try:
            data = json.loads(_get(_SUBMISSIONS_URL.format(cik=cik)))
        except Exception:
            return []
        recent = data.get("filings", {}).get("recent", {})
        out: list[FilingItem] = []
        forms_set = {f.upper() for f in forms}
        for form, date, accession in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
        ):
            if form.upper() not in forms_set:
                continue
            out.append(FilingItem(
                timestamp=datetime.strptime(date, "%Y-%m-%d"),
                symbol=ticker.upper(),
                form=form,
                accession=accession,
                url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
            ))
            if len(out) >= limit:
                break
        return out
