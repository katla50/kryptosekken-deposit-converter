# Kryptosekken deposit-konverterer

Konverterer `deposit`-transaksjoner fra en **kryptokort-eksport (CSV)** til
**Kryptosekken** sitt generiske importformat.

En `deposit`-rad betyr at kryptovaluta (f.eks. BTC) er vekslet til euro.
Skatte­messig er det et **salg av krypto**, så skriptet fører det som en handel:

* **Ut** = kryptomengden som selges (f.eks. BTC)
* **Inn** = euro-beløpet omregnet til **NOK** med Norges Banks historiske
  EUR/NOK-kurs på transaksjonsdagen
* **Type** = `Handel`

Kortkjøp (`purchase`-radene) brukes ikke — de er forbruk, ikke salg av krypto.

---

## Krav

* Python 3.9 eller nyere
* `pandas` og `requests` (se `requirements.txt`)
* Nettverkstilgang for å hente valutakurser fra Norges Bank
  (kan omgås med `--exr-file`, se under)

## Installasjon

```bash
git clone https://github.com/katla50/kryptosekken-deposit-converter.git
cd kryptosekken-deposit-converter

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Bruk

```bash
python kryptosekken_deposit_converter.py \
    --input transactions.csv \
    --output kryptosekken_deposit_import.csv
```

Skriptet skriver en oppsummering til konsoll og en importfil til `--output`.
Last importfilen opp i Kryptosekken via **Generisk CSV/Excel-import**:

> https://www.kryptosekken.no/regnskap/importer-csv-generisk

### Prøv med eksempelfilene (uten nettverk)

```bash
python kryptosekken_deposit_converter.py \
    --input examples/transactions_eksempel.csv \
    --output /tmp/ut.csv \
    --exr-file examples/EXR_eksempel.csv
```

### Alle argumenter

| Argument | Standard | Beskrivelse |
|---|---|---|
| `-i`, `--input` | `transactions.csv` | Inndatafil (kryptokort-eksport) |
| `-o`, `--output` | `kryptosekken_deposit_import.csv` | Utdatafil i Kryptosekken-format |
| `--timezone` | `Europe/Oslo` | Tidssone for `Tidspunkt`-feltet. Kilden er UTC; norsk tid brukes for både tidsstempel og kursdato |
| `--marked` | `Ukjent` | Verdi i `Marked`-kolonnen, f.eks. navnet på tjenesten kortet kommer fra |
| `--exr-file` | – | Bruk en lokalt lagret `EXR.csv` fra Norges Bank i stedet for API-kall |
| `-v`, `--verbose` | av | Detaljert logging (DEBUG) |

Eksempel med egne valg:

```bash
python kryptosekken_deposit_converter.py \
    --input ~/eksport.csv \
    --output ~/kryptosekken_2026.csv \
    --marked "MittKort" \
    --timezone Europe/Oslo
```

---

## Kryptosekken-formatet

Utdatafilen er komma-separert UTF-8 med disse kolonnene, i denne rekkefølgen:

| Kolonne | Innhold i denne konverteringen |
|---|---|
| `Tidspunkt` | `YYYY-MM-DD HH:MM:SS` i valgt tidssone |
| `Type` | `Handel` |
| `Inn` | NOK-beløp (2 desimaler) |
| `Inn-Valuta` | `NOK` |
| `Ut` | Kryptomengde |
| `Ut-Valuta` | Kryptosymbol, f.eks. `BTC` |
| `Gebyr` | tomt (kilden oppgir ikke gebyrer) |
| `Gebyr-Valuta` | tomt |
| `Marked` | verdien fra `--marked` |
| `Notat` | kildebeløp i EUR, kurs og kursdato, samt kildens transaksjons-ID |

Gyldige `Type`-verdier hos Kryptosekken er blant annet `Handel`, `Erverv`,
`Mining`, `Inntekt`, `Tap`, `Forbruk`, `Renteinntekt`, `Overføring-Inn`,
`Overføring-Ut`, `Gave-Inn`, `Gave-Ut`, `Tap-uten-fradrag` og
`Forvaltningskostnad`.

## Valutakurser

Kursene hentes fra Norges Banks åpne API:

```
https://data.norges-bank.no/api/data/EXR/B.<VALUTA>.NOK.SP?format=csv
```

* Hele perioden hentes i ett kall per valuta.
* Finnes det ingen kurs for dagen (helg/helligdag), brukes nærmeste
  **tidligere** virkedag. Finnes det ingen tidligere kurs, brukes nærmeste
  senere — og det logges en advarsel.
* Alle avvik fra eksakt kursdato logges til konsoll.
* Har du lastet ned `EXR.csv` manuelt fra
  [Norges Banks kvoteringstjeneste](https://app.norges-bank.no/query/index.html#/no/currency),
  kan den brukes med `--exr-file` (semikolon-separert, `BASE_CUR`/`QUOTE_CUR`/
  `TIME_PERIOD`/`OBS_VALUE`).

## Feilhåndtering

| Situasjon | Oppførsel |
|---|---|
| Inndatafilen finnes ikke | Tydelig feilmelding, returkode 1 |
| Påkrevde kolonner mangler | Feilmelding som lister opp hvilke kolonner som mangler |
| Ingen `deposit`-rader | Feilmelding |
| Dupliserte `deposit`-rader (samme transaksjons-ID) | Duplikatene fjernes og det logges en advarsel |
| Ugyldig dato, beløp eller kryptomengde | Feilmelding med transaksjons-ID |
| Ingen kurs for en dato | Nærmeste virkedag brukes, med advarsel |
| Ingen kurs i det hele tatt | Feilmelding |
| Nettverksfeil mot Norges Bank | Feilmelding med tips om `--exr-file` |

Merk: kildeeksporten kan ha komma inne i merchant-navnet (f.eks.
`BUTIKK 1, BY`). Skriptet reparerer slike rader automatisk.

## Tester

```bash
pip install pytest
pytest
```

Testene kjører uten nettverk, med eksempelfilene i `examples/`.

## Forbehold

Skriptet er laget for å gjøre konverteringen rask og sporbar, men det tar
ingen stilling til din skattesituasjon. Kontroller alltid rader og beløp mot
kilden din før du importerer, og sjekk at `Marked`, tidssone og kursvalg
passer for deg. Ved tvil: snakk med en regnskapsfører eller skatteadvokat.

## Lisens

MIT, se `LICENSE`.
