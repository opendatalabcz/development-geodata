# Historie stavebních objektů z RÚIAN

Bakalářská práce – sestavení verzované historie stavebních objektů z měsíčních
snapshotů RÚIAN VFR (ČÚZK).

```
pip install -r navrh/requirements.txt
python -m navrh.pipeline.build_region --municipality 539309
```

Podrobnosti k pipelině (rozsah měsíců, navazování běhů, výstupy) jsou v
[navrh/pipeline/README.md](navrh/pipeline/README.md).

- `navrh/pipeline/` – vlastní pipeline (stažení, parsování, historie, exporty)
- `analyza/` – průzkumové notebooky, profiling zdrojových dat, číselníky
- `research/` – poznámky ke zdrojům a datovým sadám
