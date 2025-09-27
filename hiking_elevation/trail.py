import csv
import logging
import math
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
    elevation_samples: list[float] = Field(default_factory=list, description="Elevation samples along trail")
    ascent: float = Field(0.0, description="Total ascent in meters")
    descent: float = Field(0.0, description="Total descent in meters")
    length: float = Field(0.0, description="Total length in km")
    _projected_geometry: LineString | None = PrivateAttr(None)
    _sample_coords: list[tuple[float, float]] = PrivateAttr(default_factory=list)
    _dem_sources: list[rasterio.io.DatasetReader] = PrivateAttr(default_factory=list)
    _first_point: tuple[float, float] = PrivateAttr(default_factory=float)
    _last_point: tuple[float, float] = PrivateAttr(default_factory=float)

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
            Sample distance in meters (1–10 m).
        """
        # Linear interpolation:
        min_dist, max_dist = 1.0, 10.0
        min_len, max_len = 1.0, 50.0
        scale = (self.length - min_len) / (max_len - min_len)
        return round(min_dist + scale * (max_dist - min_dist))

    def _project_trail_to_dem(self, dem_crs) -> LineString:
        """Project trail geometry to DEM CRS and store it."""
        project = pyproj.Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True).transform
        self._projected_geometry = transform(project, self.geometry)
        return self._projected_geometry

    def _generate_sample_coords(self, sample_distance: float) -> list[tuple[float, float]]:
        """Generate interpolated coordinates along the projected trail."""
        if self._projected_geometry is None:
            raise ValueError("Trail geometry not projected yet.")
        n_points = max(int(self._projected_geometry.length / sample_distance), 2)
        self._sample_coords = [
            self._projected_geometry.interpolate(i / (n_points - 1), normalized=True).coords[0]
            for i in range(n_points)
        ]
        return self._sample_coords

    def _open_dem_files(self, dem_paths: list[Path]):
        """Open DEM files and store references."""
        self._dem_sources = [rasterio.open(p) for p in dem_paths]
        return self._dem_sources

    def _close_dem_files(self):
        """Close opened DEM files."""
        for src in self._dem_sources:
            src.close()
        self._dem_sources.clear()

    def _sample_elevations_from_coords(self) -> np.ndarray:
        """Sample elevations for coordinates using opened DEMs."""
        elevations = []
        for x, y in self._sample_coords:
            val = np.nan
            for src in self._dem_sources:
                if src.bounds.left <= x <= src.bounds.right and src.bounds.bottom <= y <= src.bounds.top:
                    try:
                        row, col = src.index(x, y)
                        val = float(src.read(1)[row, col])
                    except rasterio.errors.RasterioIOError as e:
                        logger.warning(f"Failed to read DEM at ({x},{y}) from {src.name}: {e}")
                        val = np.nan
                    break
            elevations.append(val)
        return np.array(elevations)

    def sample_elevations(self, dem_paths: list[Path]):
        """Main method to sample elevations along the trail."""
        if self.geometry is None:
            raise ValueError("Trail.geometry is None. Fetch geometry first.")
        if not dem_paths:
            raise ValueError("No DEM paths provided")

        # 1) Project trail to DEM CRS
        with rasterio.open(dem_paths[0]) as src:
            dem_crs = src.crs
        self._project_trail_to_dem(dem_crs)

        # 2) Generate sample coordinates
        self._generate_sample_coords(self.interpolated_sample_distance)

        # 3) Open DEM files
        self._open_dem_files(dem_paths)

        try:
            # 4) Sample elevations
            elevations = self._sample_elevations_from_coords()

            # 5) Handle missing values
            nans = np.isnan(elevations)
            not_nans = ~nans
            if np.any(nans):
                if np.any(not_nans):
                    elevations[nans] = np.interp(np.flatnonzero(nans), np.flatnonzero(not_nans),
                                                 elevations[not_nans])
                else:
                    logger.warning(
                        f"All elevation samples are missing for trail {self.wikidata_qid}. Filling with zeros.")
                    elevations[:] = 0

            self.elevation_samples = elevations.tolist()

        finally:
            # 6) Close DEM files
            self._close_dem_files()

    def compute_ascent_descent(self):
        logger.debug("_compute_ascent_descent: running")
        diffs = np.diff(self.elevation_samples)
        self.ascent = float(diffs[diffs > 0].sum())
        self.descent = float(-diffs[diffs < 0].sum())
        print(
            f"Sampled {len(self.elevation_samples)} elevation points. "
            f"Ascent: {self.ascent:.1f} m, Descent: {self.descent:.1f} m")

    def fetch_geometry(self) -> None:
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

    def _update_first_last_points(self) -> None:
        """Update first and last coordinate tuples (lon, lat) from geometry."""
        if self.geometry is None:
            raise ValueError("Geometry is not set. Run fetch_geometry first.")
        coords = list(self.geometry.coords)
        if not coords:
            raise ValueError("Geometry has no coordinates")
        self._first_point = coords[0]  # (lon, lat)
        self._last_point = coords[-1]  # (lon, lat)

    def _compute_direction(self) -> str | None:
        """
        Compute cardinal direction from first_point → last_point.
        Returns one of [N, NE, E, SE, S, SW, W, NW] or None if same point.
        """
        if not self._first_point or not self._last_point:
            return None

        lon1, lat1 = self._first_point
        lon2, lat2 = self._last_point

        # roundtrip check
        if math.isclose(lat1, lat2, abs_tol=1e-5) and math.isclose(lon1, lon2, abs_tol=1e-5):
            return None

        dlon = lon2 - lon1
        dlat = lat2 - lat1

        angle = math.degrees(math.atan2(dlon, dlat))  # atan2(x, y) → bearing
        bearing = (angle + 360) % 360

        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        idx = round(bearing / 45) % 8
        return directions[idx]

    def save_to_csv(self, path: Path = config.csv_path) -> None:
        """
        Save trail stats to a CSV file with columns:
        Appends to the file if it exists.
        """
        path = Path(path)
        is_new = not path.exists()

        # Ensure first/last points are updated before saving
        if self.geometry is not None:
            self._update_first_last_points()

        direction = self._compute_direction()

        with path.open("a", newline="") as csvfile:
            writer = csv.writer(csvfile)
            # Write header if new file
            if is_new:
                writer.writerow([
                    "qid", "osm_id", "date",
                    "ascent", "descent", "samples", "sample_distance", "osm_length",
                    "first_lat", "first_lon", "last_lat", "last_lon",
                    "direction"
                ])
            writer.writerow([
                self.wikidata_qid,
                self.osm_relation_id,
                datetime.now().date().isoformat(),
                round(self.ascent, 2),
                round(self.descent, 2),
                len(self.elevation_samples),
                self.interpolated_sample_distance,
                self.si_length_in_osm,
                self._first_point[1] if self._first_point else None,  # lat
                self._first_point[0] if self._first_point else None,  # lon
                self._last_point[1] if self._last_point else None,  # lat
                self._last_point[0] if self._last_point else None,  # lon
                direction
            ])
