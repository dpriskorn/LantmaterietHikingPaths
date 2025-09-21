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
from wikibaseintegrator import WikibaseIntegrator
from wikibaseintegrator.datatypes import Item, Quantity
from wikibaseintegrator.models import Reference, Claim

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

    def sample_elevations(self, dem_paths: list[Path], sample_distance: float = 5.0):
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
        n_points = max(int(trail_proj.length / sample_distance), 2)
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

        # Compute ascent/descent
        self._compute_ascent_descent()

    # Privat metod för ascent/descent
    def _compute_ascent_descent(self):
        logger.debug("_compute_ascent_descent: running")
        diffs = np.diff(self.elevations)
        self.ascent = float(diffs[diffs > 0].sum())
        self.descent = float(-diffs[diffs < 0].sum())
        print(
            f"Sampled {len(self.elevations)} elevation points. "
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
    def wikidata_url(self):
        return f"https://www.wikidata.org/wiki/{self.wikidata_qid}"

    def add_quantity_claim_with_qualifiers(self, property_id, value, unit_qid):
        # Qualifiers
        determination_method_qid = "Q43229"  # digital elevation model
        point_spacing_m = 5
        number_of_points = len(self.elevations)
        calculation_date = datetime.date(datetime.now()).isoformat()

        # Reference
        source_item_qid = "Q136346193"
        retrieved_date = datetime.date(datetime.now()).isoformat()
        # todo
        claim = Claim(property=property_id, value=wbi_functions.Quantity(amount=value, unit=unit_qid))

        # Add qualifiers
        # Determination method
        claim.add_qualifier(qual_property="P459", value=Item(determination_method_qid))
        # Point spacing
        claim.add_qualifier(qual_property="P2283", value=Quantity(amount=point_spacing_m, unit="Q11573"))
        # Number of observations
        claim.add_qualifier(qual_property="P4597", value=number_of_points)
        # Date of determination
        claim.add_qualifier(qual_property="P585", value=calculation_date)

        # Add reference
        reference = Reference()
        reference.add_value(prop="P248", value=Item(source_item_qid))  # stated in
        reference.add_value(prop="P813", value=retrieved_date)  # retrieved
        claim.add_reference(reference)

        # Add claim to item
        item.add_claim(claim)

        # Load the trail item
        wbi = WikibaseIntegrator()
        trail_item = wbi.item(trail_qid)

        # Add ascent
        add_quantity_claim_with_qualifiers(trail_item, property_id="P1038", value=ascent_m, unit_qid="Q11573")

        # Add descent
        add_quantity_claim_with_qualifiers(trail_item, property_id="P2044", value=descent_m, unit_qid="Q11573")

        # Write back to Wikidata
        trail_item.write()
