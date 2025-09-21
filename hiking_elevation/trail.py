import logging
from pathlib import Path

import numpy as np
import overpy
import rasterio
from pydantic import BaseModel, Field, PrivateAttr
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

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

    def sample_elevations(self, dem_paths: list[Path], sample_distance: float = 25.0):
        if self.geometry is None:
            raise ValueError("Trail.geometry is None. Fetch geometry first.")

        n_points = max(int(self.geometry.length / sample_distance), 2)
        coords = [self.geometry.interpolate(i / (n_points - 1), normalized=True).coords[0]
                  for i in range(n_points)]

        srcs = [rasterio.open(p) for p in dem_paths]
        elevs = []
        for x, y in coords:
            val = None
            for src in srcs:
                if src.bounds.left <= x <= src.bounds.right and src.bounds.bottom <= y <= src.bounds.top:
                    row, col = src.index(x, y)
                    val = float(src.read(1)[row, col])
                    break
            if val is None:
                val = 0.0
            elevs.append(val)
        for s in srcs: s.close()

        self.elevations = elevs
        self._compute_ascent_descent()

    # Privat metod för ascent/descent
    def _compute_ascent_descent(self):
        logger.debug("_compute_ascent_descent: running")
        diffs = np.diff(self.elevations)
        self.ascent = float(diffs[diffs > 0].sum())
        self.descent = float(-diffs[diffs < 0].sum())
        print(f"Calculated ascent: {self.ascent}m, descent: {self.descent}m")

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
    def wikidata_url(self):
        return f"https://www.wikidata.org/wiki/{self.wikidata_qid}"