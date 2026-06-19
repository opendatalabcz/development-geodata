**RÚIAN (Registr územní identifikace, adres a nemovitostí)**

Registr územní identifikace, adres a nemovitostí (RÚIAN) je jedním ze základních registrů veřejné správy. Je veřejným seznamem, nevede žádné osobní údaje a je jedinečným zdrojem adres nejen pro veřejnou správu. Obsahuje také údaje o územních prvcích, územně evidenčních jednotkách a jejich vzájemných vazbách.
https://cuzk.gov.cz/ruian/RUIAN.aspx

Poskytování údajů z RÚIAN (veřejný seznam)
- VDP: 
Aplikace Veřejný dálkový přístup k datům RÚIAN (VDP) umožňuje nahlížet a získávat data základního registru RÚIAN a také některá data editačního agendového informačního systému územní identifikace (ISÚI) a informačního systému katastru nemovitostí (ISKN).
 https://vdp.cuzk.cz 

- Služby: 
Vyhledávací, Čtecí, Ověřovací, Změnové služby: přístup mají úřady a oprávněné osoby přes CzechPOINT
https://www.szrcr.cz/cs/sluzby/spravci-a-vyvojari.

- VFR: 
Jednou z forem poskytování dat RÚIAN je jejich předávání ve formě souborů obsahujících data RÚIAN nebo ISÚI ve výměnném formátu RÚIAN (VFR). VFR jsou poskytovány ve formátu GML 3.2.1.
Soubory VFR je možné stahovat:
    - prostřednictvím aplikace Veřejný dálkový přístup (VDP) – volně dostupné pro všechny,
    - z internetových (URL) adres vrácených službami ISZR ruianSouboryDat a ruianSouboryZmen – dostupné pouze orgánům státní správy a samosprávy.
Veškeré informace o VFR naleznete zde: www.cuzk.cz/vfr

- CSV: 
Další formou poskytování údajů je seznam adresních míst RÚIAN ve formátu CSV. Soubory jsou rozděleny po obcích a jsou generovány měsíčně ze stavového VFR.
Stránku, na které máte možnost stahovat soubory, naleznete zde: http://nahlizenidokn.cuzk.cz/StahniAdresniMistaRUIAN.aspx otevření v novém okně

https://cuzk.gov.cz/ruian/RUIAN/Informace-o-RUIAN.aspx


GML = XML pro geografické aplikace

dokument popisující formát VFR:
https://cuzk.gov.cz/ruian/Poskytovani-udaju-ISUI-RUIAN-VDP/Vymenny-format-RUIAN-(VFR)/DL058RR2-v5-0-Struktura-a-popis-VFR_final.aspx

Měsíční stavové soubory jsou poskytovány pro základní datovou sadu i pro kompletní datovou sadu (viz 
dále) a jsou generovány buď pro územní prvky od státu až po obec nebo od státu až po ZSJ, v rozsahu celé ČR. Pro každou jednotlivou obec je pak generována vlastní základní a kompletní datová sada pro 
prvky obec a nižšší. Měsíční stavové soubory se generují vždy poslední den měsíce a jsou k dispozici 
následující den, tz. první den následujícího měsíce. 

Denní změnové soubory jsou k dispozici pro základní i kompletní datovou sadu, v rozsahu celé ČR 
(všechny prvky od státu až po adresní místo). Změnová data prvků mají shodnou strukturu jako stavová 
data. Pokud dojde ke změně (byť pouze jednoho atributu), do změnových dat se dostane celý prvek, resp. 
všechny jeho atributy. 
Změnové soubory jsou generovány každý den. Ovšem pouze v případě, kdy jsou k dispozici nějaké změny, 
tj. prázdný změnový soubor se nevytváří. 

jsou dostupné na: https://services.cuzk.gov.cz/vfr

---

**Český úřad zeměměřický a katastrální (ČÚZK)** poskytuje vedle RÚIANu další zásadní datové sady jako otevřená data:

ZABAGED® (Základní báze geografických dat): Digitální topografický model ČR. Obsahuje detailní prostorové informace o budovách (jejich obvody) a silniční či říční síti. Na rozdíl od RÚIANu se ZABAGED zaměřuje čistě na geografickou realitu (jak budova vypadá v terénu), nikoli na její právní/evidenční stav.

Ortofotomapa ČR: ČÚZK pravidelně aktualizuje letecké snímky celého území státu a poskytuje je jako otevřená data (často skrze prohlížecí služby WMS/WMTS). Historické ortofotomapy jsou neocenitelné pro vizuální ověření, zda budova v daném roce skutečně stála, což můžeš skvěle využít v diskuzi k experimentálnímu ověření.  

Digitální model terénu (DMR) a povrchu (DMP): Data z laserového skenování (LiDAR), která umožňují analyzovat výškové uspořádání zástavby a terénu.

---

**OpenStreetMap (OSM)**

---
další dostupná geodata:

- Městské geoportály: např. IPR Praha, KAM Brno / Geoportál Brno, Ostrava (MAPPA), Plzeň (Správa informačních technologií)

- Copernicus (Satelity Sentinel): Evropský program, jehož družice (především Sentinel-2) snímají Zemi každých několik dní v různých spektrech. Jsou ideální pro analýzu rozšiřování městských oblastí (tzv. urban sprawl) nebo detekci úbytku zeleně na úkor zástavby.

- Urban Atlas / Corine Land Cover: Evropské projekty (v rámci služby Copernicus Land Monitoring Service), které poskytují hotové klasifikované mapy využití území (Land Use/Land Cover). Sledují, jak se mění podíl zastavěných ploch ku zemědělské půdě či lesům v čase.
  
- Český statistický úřad (ČSÚ): Publikuje výsledky Sčítání lidu, domů a bytů, data jsou prostorově agregovaná (např. na úrovni základních sídelních jednotek – ZSJ). Obsahují socioekonomické informace (např. věková struktura obyvatel v dané lokalitě, obydlenost domů)
