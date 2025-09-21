# LantmäterietHikingPaths
## Hiking elevation
This pipeline fetches Swedish trails from Wikidata 
and based on their geometry downloads the DEM data from
lantmäteriet to calculate ascent and descent along the route.

```
hiking_elevation/
├── pyproject.toml           # poetry-konfiguration
├── README.md
├── poetry.lock
├── hiking_elevation/
│   ├── __init__.py
│   ├── config.py            # API-nycklar, paths
│   ├── models.py            # Pydantic-modeller
│   ├── wikidata.py          # hämta OSM-länk från Wikidata
│   ├── osm.py               # hämta geometri via Overpass
│   ├── dem.py               # hämta höjddata via STAC
│   ├── profile.py           # beräkna ascent/descent
│   └── main.py              # orchestrator
├── data/                    # temporär lagring av DEM
└── tests/
    └── test_profile.py
```

## Topografi 50 extraction 
Script to extract, filter and convert hiking paths from the 
product Topografi 50 from swedish state Lantmäteriet to WSG84 
geojson easily usable in JOSM

### Usage
 $ python extract_hiking_paths.py -i file.gpkg -o file.geojson

### Credits
It was made with help from chatgpt may 24 2023.