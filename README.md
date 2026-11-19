# Historie stavebních objektů z RÚIAN

Bakalářská práce – sestavení verzované historie stavebních objektů z měsíčních
snapshotů RÚIAN VFR (ČÚZK).

```
pip install -e .
python -m ruian_history.pipeline.build_region --municipality 539309
```

Data (history state, exports, cleaned exports) goes to `./data/`; set
`RUIAN_DATA_DIR` to use another directory.

Podrobnosti k pipelině (rozsah měsíců, navazování běhů, výstupy) jsou v
[src/ruian_history/pipeline/README.md](src/ruian_history/pipeline/README.md).

- `src/ruian_history/pipeline/` – vlastní pipeline (stažení, parsování, historie, exporty)
- `src/ruian_history/cleaning/` – čištění výstupu pipeliny ([README](src/ruian_history/cleaning/README.md)), včetně číselníků
- `analyza/` – průzkumové notebooky, profiling zdrojových dat
- `research/` – poznámky ke zdrojům a datovým sadám
