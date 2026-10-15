#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Konverterer en kryptokort-eksport (CSV) til Kryptosekken sitt generiske
importformat.

Bakgrunn
--------
Kilden er en CSV-eksport fra et kryptokort der hver rad har typen
``purchase`` (kortkjøp i EUR) eller ``deposit`` (BTC vekslet til EUR).
Kun radene med type ``deposit`` er relevante for skatt: de representerer
et *salg av kryptovaluta mot euro*. Euro-beløpet regnes om til NOK med
Norges Banks historiske EUR/NOK-kurs på transaksjonsdagen, og resultatet
føres som en handel i Kryptosekken: krypto ut, NOK inn.

Formatet som skrives er Kryptosekken sin generiske CSV-import
(https://www.kryptosekken.no/regnskap/importer-csv-generisk) med kolonnene:

    Tidspunkt, Type, Inn, Inn-Valuta, Ut, Ut-Valuta, Gebyr, Gebyr-Valuta,
    Marked, Notat

Type ``Handel`` brukes fordi det er en veksling mellom to valutaer
(BTC -> NOK). Gebyr står tomt fordi kilden ikke oppgir gebyrer.

Bruk
----
    python kryptosekken_deposit_converter.py \
        --input transactions.csv \
        --output kryptosekken_deposit_import.csv

Se README.md for fullstendig dokumentasjon.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

# pandas og requests importeres med en vennlig feilmelding, slik at
# brukeren får beskjed om hva som mangler i stedet for en stacktrace.
try:
    import pandas as pd
except ImportError:  # pragma: no cover - avhenger av miljøet
    sys.stderr.write(
        "FEIL: pandas er ikke installert.\n"
        "Installer avhengighetene med:  pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

try:
    import requests
except ImportError:  # pragma: no cover - avhenger av miljøet
    sys.stderr.write(
        "FEIL: requests er ikke installert.\n"
        "Installer avhengighetene med:  pip install -r requirements.txt\n"
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

# Norges Banks åpne API (SDMX). B = virkedager, SP = spotkurs.
# Eksempel: https://data.norges-bank.no/api/data/EXR/B.EUR.NOK.SP?format=csv
NORGES_BANK_URL = "https://data.norges-bank.no/api/data/EXR/B.{currency}.NOK.SP"

# Kolonnene Kryptosekken forventer i den generiske CSV-importen.
KRYPTOSEKKEN_COLUMNS = [
    "Tidspunkt",
    "Type",
    "Inn",
    "Inn-Valuta",
    "Ut",
    "Ut-Valuta",
    "Gebyr",
    "Gebyr-Valuta",
    "Marked",
    "Notat",
]

# Alle kolonnene i kildeeksporten (brukes til å reparere rader med
# komma inne i merchant-navnet).
EXPECTED_INPUT_COLUMNS = [
    "Transaction Type",
    "Transaction ID",
    "Date",
    "Account Amount",
    "Account Currency",
    "Crypto Amount",
    "Cryptocurrency",
    "Purchase Source",
    "Merchant Name",
]

# Kolonnene som må finnes i kildefilen.
REQUIRED_INPUT_COLUMNS = [
    "Transaction Type",
    "Transaction ID",
    "Date",
    "Account Amount",
    "Account Currency",
    "Crypto Amount",
    "Cryptocurrency",
]

# Transaksjonstypen i kilden som skal konverteres.
DEPOSIT_TYPE = "deposit"

# Kryptosekken-transaksjonstypen vi fører salget som.
KRYPTOSEKKEN_TYPE = "Handel"

# NOK avrundes til to desimaler.
NOK_QUANTUM = Decimal("0.01")

# Hvor mange dager tilbake vi leter etter en kurs hvis dagen selv er
# helligdag/helg. Kurser hentes for hele perioden på én gang, så dette er
# bare et sikkerhetsnett.
MAX_RATE_LOOKBACK_DAYS = 14

LOGGER = logging.getLogger("kryptosekken-konverterer")


# ---------------------------------------------------------------------------
# Hjelpefunksjoner
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False) -> None:
    """Setter opp logging til konsoll (INFO, eller DEBUG med --verbose)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stdout,
    )


def to_decimal(value) -> Decimal:
    """
    Gjør om en verdi til Decimal på en trygg måte.

    Vi går via tekst for å unngå flyttallfeil (f.eks. 0.00139969 blir
    aldri helt riktig som float). Returnerer None hvis verdien ikke kan
    tolkes som et tall.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "-"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def format_amount(amount: Decimal, decimals: Optional[int] = None) -> str:
    """
    Formaterer et Decimal uten vitenskapelig notasjon og uten fortegn.

    ``decimals`` tvinger et fast antall desimaler (brukes for NOK, som alltid
    føres med to desimaler). Uten ``decimals`` fjernes overflødige nuller,
    slik at kryptomengden står så nær kilden som mulig.
    """
    if decimals is not None:
        return f"{amount:.{decimals}f}"
    return format(amount.normalize(), "f")


# ---------------------------------------------------------------------------
# Innlesing av kilden
# ---------------------------------------------------------------------------


def read_deposits(input_path: Path) -> pd.DataFrame:
    """
    Leser kilde-CSV-en og returnerer kun deposit-radene.

    Feilhåndtering:
      * filen finnes ikke      -> FileNotFoundError med tydelig melding
      * kolonner mangler       -> ValueError som lister opp hva som mangler
      * ingen deposit-rader    -> ValueError

    Duplikater fjernes på ``Transaction ID``. Kilden kan inneholde samme
    transaksjon flere ganger (f.eks. hvis eksporten er limt sammen to
    ganger); uten dette ville beløpene blitt dobbeltført.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Inndatafilen finnes ikke: {input_path}")

    # Noen rader i kilden har komma inne i merchant-navnet (f.eks.
    # "BUTIKK 1, BY") og får dermed ett felt for mye. Siden merchant-navnet
    # er siste kolonne, limer vi sammen overskytende felt tilbake dit.
    def repair_bad_line(fields):
        last_index = len(EXPECTED_INPUT_COLUMNS) - 1
        if len(fields) > len(EXPECTED_INPUT_COLUMNS):
            return fields[:last_index] + [",".join(fields[last_index:])]
        return fields

    df = pd.read_csv(
        input_path,
        dtype=str,
        keep_default_na=False,
        engine="python",
        on_bad_lines=repair_bad_line,
    )
    df.columns = [c.strip() for c in df.columns]

    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Inndatafilen mangler disse kolonnene: " + ", ".join(missing)
        )

    # Fjern helt tomme rader og eventuell bunntekst fra eksporten.
    df = df[df["Transaction Type"].str.strip() != ""].copy()

    deposits = df[df["Transaction Type"].str.strip().str.lower() == DEPOSIT_TYPE].copy()
    if deposits.empty:
        raise ValueError(
            f"Fant ingen rader med Transaction Type = '{DEPOSIT_TYPE}' i {input_path}"
        )

    before = len(deposits)
    deposits = deposits.drop_duplicates(subset=["Transaction ID"], keep="first")
    removed = before - len(deposits)
    if removed:
        LOGGER.warning(
            "Fjernet %d dupliserte deposit-rader (samme Transaction ID). "
            "Uten dette ville beløpene blitt dobbeltført.",
            removed,
        )

    LOGGER.info("Leste %d unike deposit-transaksjoner fra %s", len(deposits), input_path)
    return deposits


# ---------------------------------------------------------------------------
# Valutakurser fra Norges Bank
# ---------------------------------------------------------------------------


def fetch_rates_from_api(
    currency: str, start: date, end: date, timeout: int = 30
) -> Dict[date, Decimal]:
    """
    Henter historiske kurser for ``currency``/NOK fra Norges Banks åpne API.

    API-et leverer semikolon-separert CSV med blant annet kolonnene
    TIME_PERIOD (dato) og OBS_VALUE (kurs). Vi henter hele perioden i ett
    kall og bygger et oppslagsverk {dato: kurs}.
    """
    url = NORGES_BANK_URL.format(currency=currency.upper())
    params = {
        "format": "csv",
        "startPeriod": start.isoformat(),
        "endPeriod": end.isoformat(),
    }
    LOGGER.info("Henter %s/NOK-kurser fra Norges Bank (%s .. %s)", currency, start, end)

    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()

    rates: Dict[date, Decimal] = {}
    for line in response.text.splitlines()[1:]:  # hopp over header
        fields = line.split(";")
        if len(fields) < 16:
            continue
        base_cur, quote_cur = fields[2], fields[4]
        period, value = fields[14], fields[15]
        if base_cur.upper() != currency.upper() or quote_cur.upper() != "NOK":
            continue
        amount = to_decimal(value.replace(",", "."))
        if amount is None:
            continue
        try:
            rates[date.fromisoformat(period)] = amount
        except ValueError:
            continue

    if not rates:
        raise ValueError(
            f"Norges Bank returnerte ingen {currency}/NOK-kurser for perioden "
            f"{start} .. {end}."
        )

    LOGGER.info("  -> %d kurser lastet ned", len(rates))
    return rates


def read_rates_from_file(path: Path, currency: str) -> Dict[date, Decimal]:
    """
    Leser kurser fra en lokalt lagret EXR.csv fra Norges Bank.

    Nyttig hvis man vil kjøre skriptet uten nettverkstilgang. Filen er
    semikolon-separert og har kolonnene BASE_CUR, QUOTE_CUR, TIME_PERIOD
    og OBS_VALUE.
    """
    if not path.exists():
        raise FileNotFoundError(f"Kursfilen finnes ikke: {path}")

    rates: Dict[date, Decimal] = {}
    text = path.read_text(encoding="utf-8-sig")
    for line in text.splitlines()[1:]:
        fields = line.split(";")
        if len(fields) < 16:
            continue
        if fields[2].upper() != currency.upper() or fields[4].upper() != "NOK":
            continue
        amount = to_decimal(fields[15].replace(",", "."))
        if amount is None:
            continue
        try:
            rates[date.fromisoformat(fields[14])] = amount
        except ValueError:
            continue

    if not rates:
        raise ValueError(f"Fant ingen {currency}/NOK-kurser i {path}")
    LOGGER.info("Leste %d %s/NOK-kurser fra %s", len(rates), currency, path)
    return rates


def build_rate_lookup(rates: Dict[date, Decimal]):
    """
    Lager en oppslagsfunksjon som finner kursen for en gitt dato.

    Rekkefølge:
      1. eksakt dato
      2. nærmeste *tidligere* virkedag (helg/helligdag)
      3. nærmeste senere virkedag (kun hvis ingen tidligere finnes)
    Returnerer (kurs, faktisk_dato) eller (None, None).
    """
    sorted_dates = sorted(rates)

    def lookup(wanted: date):
        if wanted in rates:
            return rates[wanted], wanted
        for offset in range(1, MAX_RATE_LOOKBACK_DAYS + 1):
            candidate = wanted - timedelta(days=offset)
            if candidate in rates:
                return rates[candidate], candidate
        for offset in range(1, MAX_RATE_LOOKBACK_DAYS + 1):
            candidate = wanted + timedelta(days=offset)
            if candidate in rates:
                return rates[candidate], candidate
        return None, None

    lookup.sorted_dates = sorted_dates  # type: ignore[attr-defined]
    return lookup


# ---------------------------------------------------------------------------
# Bygging av Kryptosekken-rader
# ---------------------------------------------------------------------------


def build_kryptosekken_rows(
    deposits: pd.DataFrame,
    rate_lookups: Dict[str, "callable"],
    timezone: str = "Europe/Oslo",
    marked: str = "Ukjent",
) -> tuple[pd.DataFrame, List[dict]]:
    """
    Bygger Kryptosekken-rader fra deposit-transaksjonene.

    Hver deposit blir en ``Handel``:
      * Ut  = kryptomengden (salg)
      * Inn = EUR-beløpet omregnet til NOK (kjøp)

    Returnerer (dataframe med importklare rader, liste med detaljer per rad
    som brukes til oppsummeringen).
    """
    tz = ZoneInfo(timezone)
    rows: List[dict] = []
    details: List[dict] = []
    missing_rates: List[str] = []

    for _, row in deposits.iterrows():
        tx_id = str(row["Transaction ID"]).strip()

        # --- tidspunkt: kilden er UTC (ISO 8601 med Z), vi fører norsk tid ---
        raw_date = str(row["Date"]).strip()
        try:
            timestamp_utc = pd.Timestamp(raw_date)
            if timestamp_utc.tzinfo is None:
                timestamp_utc = timestamp_utc.tz_localize("UTC")
        except (ValueError, TypeError):
            raise ValueError(
                f"Kunne ikke tolke datoen '{raw_date}' (Transaction ID {tx_id}). "
                "Forventet format er YYYY-MM-DDTHH:MM:SS.sssZ."
            )
        timestamp_local = timestamp_utc.tz_convert(tz)
        trade_date = timestamp_local.date()

        # --- beløp ---
        currency = str(row["Account Currency"]).strip().upper()
        crypto_symbol = str(row["Cryptocurrency"]).strip().upper()
        crypto_amount = to_decimal(row["Crypto Amount"])
        account_amount = to_decimal(row["Account Amount"])

        if account_amount is None or account_amount <= 0:
            raise ValueError(
                f"Ugyldig Account Amount for Transaction ID {tx_id}: "
                f"'{row['Account Amount']}'"
            )
        if crypto_amount is None or crypto_amount <= 0:
            raise ValueError(
                f"Ugyldig Crypto Amount for Transaction ID {tx_id}: "
                f"'{row['Crypto Amount']}'"
            )
        if currency not in rate_lookups:
            raise ValueError(
                f"Ingen kurser tilgjengelig for valutaen '{currency}' "
                f"(Transaction ID {tx_id})."
            )

        # --- valutakurs ---
        rate, rate_date = rate_lookups[currency](trade_date)
        if rate is None:
            missing_rates.append(f"{tx_id} ({trade_date})")
            continue
        if rate_date != trade_date:
            LOGGER.warning(
                "Ingen %s/NOK-kurs for %s (Transaction ID %s) - bruker "
                "nærmeste tilgjengelige kurs fra %s.",
                currency,
                trade_date,
                tx_id,
                rate_date,
            )

        # --- omregning EUR -> NOK ---
        nok_amount = (account_amount * rate).quantize(NOK_QUANTUM, rounding=ROUND_HALF_UP)

        rows.append(
            {
                "Tidspunkt": timestamp_local.strftime("%Y-%m-%d %H:%M:%S"),
                "Type": KRYPTOSEKKEN_TYPE,
                "Inn": format_amount(nok_amount, decimals=2),
                "Inn-Valuta": "NOK",
                "Ut": format_amount(crypto_amount),
                "Ut-Valuta": crypto_symbol,
                "Gebyr": "",
                "Gebyr-Valuta": "",
                "Marked": marked,
                "Notat": (
                    f"Deposit: salg av {crypto_symbol} for {format_amount(account_amount)} "
                    f"{currency} @ {rate} {currency}/NOK (Norges Bank {rate_date}). "
                    f"Kilde-ID {tx_id}"
                ),
            }
        )
        details.append(
            {
                "tidspunkt": timestamp_local.strftime("%Y-%m-%d %H:%M:%S"),
                "currency": currency,
                "account_amount": account_amount,
                "crypto_symbol": crypto_symbol,
                "crypto_amount": crypto_amount,
                "rate": rate,
                "rate_date": rate_date,
                "nok": nok_amount,
            }
        )

    if missing_rates:
        raise ValueError(
            "Fant ingen valutakurs for disse transaksjonene: "
            + ", ".join(missing_rates)
        )

    result = pd.DataFrame(rows, columns=KRYPTOSEKKEN_COLUMNS)
    # Eldste transaksjon først, slik Kryptosekken forventer det.
    if not result.empty:
        result = result.sort_values("Tidspunkt", kind="stable").reset_index(drop=True)
        details.sort(key=lambda d: d["tidspunkt"])

    return result, details


# ---------------------------------------------------------------------------
# Skriving og oppsummering
# ---------------------------------------------------------------------------


def write_output(df: pd.DataFrame, output_path: Path) -> None:
    """Skriver importfilen til Kryptosekken (UTF-8, komma-separert)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8", lineterminator="\n")
    LOGGER.info("Skrev %d rader til %s", len(df), output_path)


def print_summary(details: List[dict], output_path: Path) -> None:
    """Skriver en oppsummering av konverteringen til konsoll."""
    if not details:
        print("\nIngen rader ble konvertert.")
        return

    totals_by_currency: Dict[str, Decimal] = {}
    for detail in details:
        totals_by_currency[detail["currency"]] = (
            totals_by_currency.get(detail["currency"], Decimal("0"))
            + detail["account_amount"]
        )

    total_nok = sum((d["nok"] for d in details), Decimal("0"))
    total_crypto: Dict[str, Decimal] = {}
    for detail in details:
        total_crypto[detail["crypto_symbol"]] = (
            total_crypto.get(detail["crypto_symbol"], Decimal("0")) + detail["crypto_amount"]
        )

    rates = [d["rate"] for d in details]
    print("\n" + "=" * 62)
    print("OPPSUMMERING")
    print("=" * 62)
    print(f"Antall rader konvertert : {len(details)}")
    print(f"Periode                 : {details[0]['tidspunkt']}  ->  {details[-1]['tidspunkt']}")
    for currency, amount in sorted(totals_by_currency.items()):
        print(f"Totalt {currency:<17}: {format_amount(amount, decimals=2)}")
    for symbol, amount in sorted(total_crypto.items()):
        print(f"Totalt {symbol:<17}: {format_amount(amount)}")
    print(f"Totalt NOK (inn)        : {format_amount(total_nok, decimals=2)}")
    print(f"Laveste/høyeste kurs    : {min(rates)} / {max(rates)}")
    print(f"Utdatafil               : {output_path}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# Kommandolinje og main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Definerer og leser kommandolinjeargumenter."""
    parser = argparse.ArgumentParser(
        description=(
            "Konverterer deposit-transaksjoner (krypto solgt for EUR) fra en "
            "kryptokort-CSV til Kryptosekken sitt generiske importformat."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Eksempel:\n"
            "  python kryptosekken_deposit_converter.py \\\n"
            "      --input transactions.csv \\\n"
            "      --output kryptosekken_deposit_import.csv\n"
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("transactions.csv"),
        help="Inndatafil med kryptokort-transaksjoner (standard: transactions.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("kryptosekken_deposit_import.csv"),
        help="Utdatafil i Kryptosekken-format (standard: kryptosekken_deposit_import.csv)",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Oslo",
        help="Tidssone for Tidspunkt-feltet (standard: Europe/Oslo)",
    )
    parser.add_argument(
        "--marked",
        default="Ukjent",
        help="Verdi i Marked-kolonnen, f.eks. navnet på tjenesten kortet kommer fra "
        "(standard: Ukjent)",
    )
    parser.add_argument(
        "--exr-file",
        type=Path,
        default=None,
        help="Bruk en lokalt lagret EXR.csv fra Norges Bank i stedet for API-kall",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Vis detaljert logging (DEBUG)"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Hovedfunksjon: leser kilde, henter kurser, skriver Kryptosekken-CSV."""
    args = parse_args(argv)
    setup_logging(args.verbose)

    try:
        # 1) Les deposit-transaksjonene fra kilden.
        deposits = read_deposits(args.input)

        # 2) Finn hvilke valutaer som faktisk brukes, og hent kurser for dem.
        currencies = sorted({str(c).strip().upper() for c in deposits["Account Currency"]})
        dates = pd.to_datetime(deposits["Date"], utc=True, format="ISO8601")
        # Hent med margin bakover, slik at vi har en tidligere virkedag til
        #gjenge når første transaksjon ligger på en helg/helligdag.
        start = (
            dates.min().tz_convert(args.timezone).date()
            - timedelta(days=MAX_RATE_LOOKBACK_DAYS)
        )
        end = dates.max().tz_convert(args.timezone).date()

        rate_lookups = {}
        for currency in currencies:
            if args.exr_file:
                rates = read_rates_from_file(args.exr_file, currency)
            else:
                rates = fetch_rates_from_api(currency, start, end)
            rate_lookups[currency] = build_rate_lookup(rates)

        # 3) Bygg Kryptosekken-radene.
        df, details = build_kryptosekken_rows(
            deposits, rate_lookups, timezone=args.timezone, marked=args.marked
        )

        # 4) Skriv utdatafil og oppsummering.
        write_output(df, args.output)
        print_summary(details, args.output)
        return 0

    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1
    except ValueError as error:
        LOGGER.error("%s", error)
        return 1
    except requests.RequestException as error:
        LOGGER.error(
            "Kunne ikke hente valutakurser fra Norges Bank: %s\n"
            "Tips: sjekk nettverket, eller bruk --exr-file med en lokalt "
            "nedlastet EXR.csv fra Norges Bank.",
            error,
        )
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.error("Avbrutt av brukeren.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
