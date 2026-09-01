# Pipeline

Sestaví verzovanou historii stavebních objektů (RÚIAN) z měsíčních snapshotů
VFR (OB_UKSH) ze `services.cuzk.gov.cz/vfr`.

Jediný vstupní bod je `build_region.py` – zvládne jednu obec, seznam obcí i
celý pojmenovaný region (viz `regions.py`), vždy pro zvolený rozsah měsíců.

```
pip install -r navrh/requirements.txt
```

## Spuštění (z kořene repozitáře)

Celá dostupná historie jedné obce (Chýně):

```
python -m navrh.pipeline.build_region --municipality 539309
```

Jen jeden měsíc – pravidelné doplnění nově zveřejněného snapshotu:

```
python -m navrh.pipeline.build_region --municipality 539309 --start 2026-7 --end 2026-7
```

Celý region:

```
python -m navrh.pipeline.build_region --region brno_area --workers 12
```

## Rozsah měsíců

`--start` / `--end` berou `YYYYMM` i `YYYY-M` (`202607` = `2026-7` = `2026-07`).

Výchozí rozsah je celá dostupná historie, tj. **201508 až poslední zveřejněný
měsíc**. Obě meze se automaticky ořežou na to, co ČÚZK skutečně vystavuje –
zdola na začátek archivu (starší snapshoty neexistují), shora na poslední
zveřejněný měsíc (ten se zjistí z výpisu `/vfr/`, jedním requestem za běh;
poslední měsíc nebo dva ještě nebývají venku). Případné oříznutí se vypíše.

Formát souborů se přes celý archiv nemění (jen přípona `.xml.gz` →
`.xml.zip`, což řeší `download.py` čtením skutečného výpisu adresáře), takže
stejný kód zpracuje snapshot z roku 2015 i z letoška.

## Navazování běhů

Stav se pro každou obec ukládá zvlášť do `navrh/data/history/<kod>.parquet`,
takže běh jde kdykoliv přerušit a znovu spustit – co je hotové, se nestahuje
znovu, a další běh s navazujícím rozsahem historii jen prodlouží.

Sestavování historie (SCD2) ale závisí na pořadí snapshotů, takže se rozsah
před během kontroluje a běh raději skončí chybou, než aby vznikla neúplná
historie:

- **rozsah zasahuje před už zpracovaný stav** – takový snapshot nejde doplnit
  dodatečně. Buď posuňte `--start`, nebo dejte `--rebuild` (postaví historii
  znovu od nuly).
- **v řadě měsíců by zůstala díra** – objekt zaniklý uvnitř díry by dostal
  `valid_to` až podle prvního snapshotu za ní. Výpis rovnou napíše, jaké
  `--start` použít, aby řada navazovala.

Obojí se týká jen měsíců, které na serveru **jsou** a jen jste si je zadaným
rozsahem nevyžádali. Spravit to jde vždycky a zadarmo – proto se díra vědomě
udělat nedá a běh v takovém případě skončí chybou; historie s dírou vypadá
jako každá jiná, ale tiše lže o tom, kdy objekty zanikly.

Měsíce, které stáhnout nejde (chybí nebo jsou poškozené), se sem nepočítají:
s těmi uživatel nic nezmůže, takže běh nezastavují – jen se na ně upozorní
(viz níž).

### Když se měsíc nepovede

Zdrojový soubor občas chybí nebo nejde zpracovat. Co s takovým měsícem bude,
se rozhodne hned podle toho, jestli se ještě může objevit:

- **měsíc starší než poslední zveřejněný** – ČÚZK zveřejňuje popořadě a starší
  měsíce zpětně nedoplňuje, takže se ten soubor už neobjeví. Měsíc se odloží
  natrvalo (`skipped_months` v `.progress.json`) a další běh ho nezkouší.
  Doplnit ho jde jedině přes `--rebuild`.
- **měsíc od posledního zveřejněného dál** – ten ČÚZK teprve vystaví, takže se
  zapíše jako čekající (`pending_months`) a každý další běh ho zkusí znovu. To
  je běžný stav při měsíčním doplňování.

Když se nepodaří načíst výpis `/vfr/`, a horní mez archivu tedy není jistá,
neodloží se natrvalo nic – všechno se zkusí příště znovu.

Odložené měsíce běh neblokují, jen se na ně upozorní. Platí to i pro obce,
které na začátku archivu ještě neexistovaly (např. pražské městské části
vzniklé až v roce 2016): jejich chybějící měsíce se po prvním běhu odloží a
výchozí rozsah od `201508` u nich funguje dál.

## Známé díry ve zdrojových datech

- **Brno (582786), 2016-05** – soubor `20160531_OB_582786_UKSH.xml.gz` na
  serveru ČÚZK je poškozený (neprojde CRC, deflate stream ztrácí synchronizaci
  zhruba v polovině, ještě před sekcí `StavebniObjekty`). Stahuje se celý a
  opakovaně bajt po bajtu stejný, takže nejde o přenos. Data za tenhle měsíc
  se pro Brno získat nedají; v historii je díra 2016-04 → 2016-06.

## Výstupy

`navrh/output/<jméno>_history.{gpkg,csv,geojson}`, kde `<jméno>` je kód obce
resp. jméno regionu (přepíše `--name`). CSV (atributy) a GeoJSON (geometrie)
jsou propojitelné přes sloupec `code`.
