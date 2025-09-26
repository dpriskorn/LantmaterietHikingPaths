import csv
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import overpy
import pyproj
import rasterio
from pydantic import BaseModel, Field, PrivateAttr
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from shapely.ops import transform

import config

logger = logging.getLogger(__name__)


class Trail(BaseModel):
    """
    Representerar en vandringsled med metadata och OSM-geometri.
    Trail kan själv hämta sin geometri från Overpass API.
    """
    # name: str = Field(..., description="Trail name")
    wikidata_qid: str = Field(..., description="Wikidata QID")
    osm_relation_id: int = Field(..., description="OpenStreetMap relation ID")
    geometry: LineString | None = Field(None, description="Trail geometry as LineString")
    elevations: list[float] = Field(default_factory=list, description="Elevation samples along trail")
    ascent: float = Field(0.0, description="Total ascent in meters")
    descent: float = Field(0.0, description="Total descent in meters")
    length: float = Field(0.0, description="Total length in km")

    # internt API-klient
    _api: overpy.Overpass = PrivateAttr(default_factory=overpy.Overpass)

    # config
    model_config = {"arbitrary_types_allowed": True}

    @property
    def interpolated_sample_distance(self) -> float:
        """
        Choose a reasonable sampling distance (in meters) based on trail length.

        Returns
        -------
        float
            Sample distance in meters (1–25 m).
        """
        # Linear interpolation:
        min_dist, max_dist = 1.0, 5.0
        min_len, max_len = 1.0, 50.0
        scale = (self.length - min_len) / (max_len - min_len)
        return min_dist + scale * (max_dist - min_dist)

    def sample_elevations(self, dem_paths: list[Path]):
        """
        Sample elevations along the trail from DEM tiles.
        Handles CRS transformation from WGS84 (OSM) to DEM CRS.
        Calculates ascent and descent.
        """
        if self.geometry is None:
            raise ValueError("Trail.geometry is None. Fetch geometry first.")

        if not dem_paths:
            raise ValueError("No DEM paths provided")

        # Open first DEM to get CRS
        with rasterio.open(dem_paths[0]) as src:
            dem_crs = src.crs

        # Transform trail geometry to DEM CRS
        project = pyproj.Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True).transform
        trail_proj = transform(project, self.geometry)

        # Interpolate points along the trail
        n_points = max(int(trail_proj.length / self.interpolated_sample_distance), 2)
        coords = [trail_proj.interpolate(i / (n_points - 1), normalized=True).coords[0]
                  for i in range(n_points)]

        # Open all DEMs
        srcs = [rasterio.open(p) for p in dem_paths]

        elevations = []
        for x, y in coords:
            val = None
            for src in srcs:
                if src.bounds.left <= x <= src.bounds.right and src.bounds.bottom <= y <= src.bounds.top:
                    row, col = src.index(x, y)
                    val = float(src.read(1)[row, col])
                    break
            if val is None:
                val = np.nan  # Use NaN to mark missing data
            elevations.append(val)

        # Close DEM files
        for src in srcs:
            src.close()

        # Replace missing values with linear interpolation if possible
        elevations = np.array(elevations)
        if np.any(np.isnan(elevations)):
            nans = np.isnan(elevations)
            not_nans = ~nans
            elevations[nans] = np.interp(np.flatnonzero(nans), np.flatnonzero(not_nans), elevations[not_nans])

        self.elevations = elevations.tolist()

    def compute_ascent_descent(self):
        logger.debug("_compute_ascent_descent: running")
        diffs = np.diff(self.elevations)
        self.ascent = float(diffs[diffs > 0].sum())
        self.descent = float(-diffs[diffs < 0].sum())
        print(
            f"Sampled {len(self.elevations)} elevation points. "
            f"Ascent: {self.ascent:.1f} m, Descent: {self.descent:.1f} m")

    def __fetch_geometry__(self) -> None:
        """
        Hämta geometrin från OSM via relation ID och uppdatera Trail.geometry.
        """
        logger.debug("fetch_geometry: running")
        query = f"""
        [out:json];
        relation({self.osm_relation_id});
        way(r);
        (._;>;);
        out geom;
        """
        result = self._api.query(query)

        coords_list = [
            [(float(node.lon), float(node.lat)) for node in way.nodes]
            for way in result.ways
        ]

        mls = MultiLineString([LineString(c) for c in coords_list])
        merged = linemerge(mls)

        # Hantera MultiLineString
        if isinstance(merged, LineString):
            self.geometry = merged
        elif isinstance(merged, MultiLineString):
            # Slå ihop alla delsträngar till en kontinuerlig LineString (om möjligt)
            all_coords = []
            for geom in merged.geoms:
                all_coords.extend(list(geom.coords))
            # ta bort dubbletter vid överlapp
            seen = set()
            dedup_coords = []
            for c in all_coords:
                if c not in seen:
                    dedup_coords.append(c)
                    seen.add(c)
            self.geometry = LineString(dedup_coords)
        else:
            raise ValueError("Kan inte skapa LineString från geometri")

    @property
    def si_length_in_osm(self) -> float:
        """
        Beräkna längden på sträckan i meter baserat på self.geometry.
        Kräver att self.geometry är en LineString.
        """
        if self.geometry is None:
            raise ValueError("Geometry är inte satt. Kör fetch_geometry först.")

        # Transformer från WGS84 (lat/lon) till ett projicerat koordinatsystem (t.ex. UTM).
        # Använder automatisk projicering beroende på geometriens centroid.
        proj = pyproj.Transformer.from_crs(
            "EPSG:4326",  # WGS84
            pyproj.CRS.from_proj4(
                f"+proj=utm +zone={int((self.geometry.centroid.x + 180) // 6) + 1} "
                f"{'+south' if self.geometry.centroid.y < 0 else ''} +datum=WGS84 +units=m +no_defs"
            ),
            always_xy=True
        ).transform

        projected = transform(proj, self.geometry)
        return round(projected.length)

    @property
    def wikidata_url(self):
        return f"https://www.wikidata.org/wiki/{self.wikidata_qid}"

    def save_to_csv(self, path: Path = config.csv_path) -> None:
        """
        Save trail stats to a CSV file with columns:
        Appends to the file if it exists.
        """
        path = Path(path)
        is_new = not path.exists()

        with path.open("a", newline="") as csvfile:
            writer = csv.writer(csvfile)
            # Write header if new file
            if is_new:
                writer.writerow(["qid", "osm_id", "date", "ascent", "descent", "num_points", "point_spacing", "osm_length_km"])
            writer.writerow([
                self.wikidata_qid,
                self.osm_relation_id,
                datetime.now().date().isoformat(),
                round(self.ascent, 2),
                round(self.descent, 2),
                len(self.elevations),
                self.interpolated_sample_distance,
                self.si_length_in_osm
            ])
