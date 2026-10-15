# -*- coding: utf-8 -*-
"""
Tester for kryptosekken_deposit_converter.

Testene kjører helt uten nettverk: de bruker eksempelfilene i examples/
og en lokalt lagret EXR.csv fra Norges Bank (--exr-file).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from kryptosekken_deposit_converter import (
    KRYPTOSEKKEN_COLUMNS,
    build_kryptosekken_rows,
    build_rate_lookup,
    main,
    read_deposits,
    read_rates_from_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_INPUT = REPO_ROOT / "examples" / "transactions_eksempel.csv"
EXAMPLE_EXR = REPO_ROOT / "examples" / "EXR_eksempel.csv"


def test_read_deposits_filters_and_deduplicates():
    """Bare deposit-rader skal med, og duplikater fjernes."""
    deposits = read_deposits(EXAMPLE_INPUT)

    assert len(deposits) == 3
    assert set(deposits["Transaction Type"]) == {"deposit"}
    assert deposits["Transaction ID"].is_unique


def test_read_deposits_missing_file():
    """Manglende inndatafil gir tydelig feilmelding."""
    with pytest.raises(FileNotFoundError, match="finnes ikke"):
        read_deposits(Path("finnes-ikke.csv"))


def test_read_deposits_missing_columns(tmp_path):
    """Manglende kolonner gir tydelig feilmelding."""
    broken = tmp_path / "broken.csv"
    broken.write_text("Transaction Type,Date\npurchase,2024-01-01\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mangler disse kolonnene"):
        read_deposits(broken)


def test_rate_lookup_falls_back_to_previous_business_day():
    """Søndag 17. mars skal bruke kursen fra fredag 15. mars."""
    from datetime import date

    rates = read_rates_from_file(EXAMPLE_EXR, "EUR")
    lookup = build_rate_lookup(rates)

    rate, rate_date = lookup(date(2024, 3, 17))
    assert rate == Decimal("11.5500")
    assert rate_date == date(2024, 3, 15)


def test_build_rows_expected_values():
    """Kontrollerer Inn (NOK), Ut (BTC), type og kolonnenavn."""
    deposits = read_deposits(EXAMPLE_INPUT)
    lookup = build_rate_lookup(read_rates_from_file(EXAMPLE_EXR, "EUR"))

    df, details = build_kryptosekken_rows(
        deposits, {"EUR": lookup}, timezone="Europe/Oslo", marked="Test"
    )

    assert list(df.columns) == KRYPTOSEKKEN_COLUMNS
    assert len(df) == 3
    assert set(df["Type"]) == {"Handel"}
    assert set(df["Inn-Valuta"]) == {"NOK"}
    assert set(df["Ut-Valuta"]) == {"BTC"}

    # Sortert eldste først, med norsk sommertid (UTC+1 i mars).
    assert df.loc[0, "Tidspunkt"] == "2024-03-04 09:55:12"
    assert df.loc[0, "Inn"] == "1150.00"
    assert df.loc[0, "Ut"] == "0.0016"

    total_nok = sum((d["nok"] for d in details), Decimal("0"))
    assert total_nok == Decimal("4922.03")


def test_main_end_to_end(tmp_path):
    """Hele skriptet kjører og skriver en importfil med riktig innhold."""
    output = tmp_path / "ut.csv"

    exit_code = main(
        [
            "--input",
            str(EXAMPLE_INPUT),
            "--output",
            str(output),
            "--exr-file",
            str(EXAMPLE_EXR),
            "--marked",
            "Test",
        ]
    )

    assert exit_code == 0
    content = output.read_text(encoding="utf-8").splitlines()
    assert content[0] == ",".join(KRYPTOSEKKEN_COLUMNS)
    assert len(content) == 4  # header + 3 transaksjoner
    assert "Handel" in content[1]


def test_main_missing_input_returns_error(tmp_path):
    """Manglende inndatafil gir returkode 1, ikke stacktrace."""
    exit_code = main(
        ["--input", str(tmp_path / "finnes-ikke.csv"), "--output", str(tmp_path / "ut.csv")]
    )
    assert exit_code == 1
