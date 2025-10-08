# LantmäterietHikingPaths
## Hiking elevation
This pipeline fetches Swedish trails from Wikidata 
and based on their geometry downloads the DEM data from
lantmäteriet to calculate ascent and descent along the route.

It excludes all trails longer than 50 km.


- **Qualifiers:**
  - *Determination method* (`P459`) → e.g., "digital elevation model" (`Q43229`)
  - *Point spacing* (`P2283`) → e.g., `5 m` (`Q11573`)
  - *Number of observations* (`P4597`) → number of elevation points
  - *Date of determination* (`P585`) → calculation date in ISO format

- **Reference:**
  - *Stated in* (`P248`) → e.g., High quality DEM dataset from Lantmäteriet (`Q136346193`)
  - *Retrieved* (`P813`) → retrieval date in ISO format

Since OSM data is used in this pipeline the result can only be released under ODblv2 license. 
It is thus not suitable for upload to Wikidata.
  
## Topografi 50 extraction 
Script to extract, filter and convert hiking paths from the 
product Topografi 50 from swedish state Lantmäteriet to WSG84 
geojson easily usable in JOSM

### Usage
 $ python extract_hiking_paths.py -i file.gpkg -o file.geojson

### Credits
It was made with help from chatgpt may 24 2023.