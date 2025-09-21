# tests/test_trail.py
from unittest import TestCase

import pytest
from shapely.geometry import LineString

from hiking_elevation.stac.orchestrator import STACOrchestrator
from hiking_elevation.trail import Trail


@pytest.mark.parametrize("wikidata_qid,osm_relation_id", [
    ("Q119950026", 12308978),
])
def test_fetch_geometry(wikidata_qid, osm_relation_id):
    trail = Trail(wikidata_qid=wikidata_qid, osm_relation_id=osm_relation_id)

    # Fetch geometry from OSM
    trail.fetch_geometry()

    # Check that geometry is a LineString and has coordinates
    assert isinstance(trail.geometry, LineString)
    assert len(trail.geometry.coords) > 0

    # Optionally, check that private _api exists
    assert hasattr(trail, "_api")

@pytest.mark.parametrize("wikidata_qid,osm_relation_id", [
    ("Q119950026", 12308978),
])
def test_trail_pipeline(wikidata_qid, osm_relation_id):
    trail = Trail(wikidata_qid=wikidata_qid, osm_relation_id=osm_relation_id)

    # -------------------------
    # Step 1: Fetch geometry
    # -------------------------
    trail.fetch_geometry()
    geometry = trail.geometry
    assert geometry.length > 0

    # -------------------------
    # Step 2: Setup STAC orchestrator
    # -------------------------
    orchestrator = STACOrchestrator()
    orchestrator.load_collections(force_refresh=False)

    # Step 3: Get DEM tiles intersecting trail bbox
    minx, miny, maxx, maxy = geometry.bounds
    dem_paths = orchestrator.get_dem_tiles_for_bbox([minx, miny, maxx, maxy])
    assert len(dem_paths) > 0

    # Step 4: Sample elevations
    trail.sample_elevations(dem_paths, sample_distance=25.0)
    assert len(trail.elevations) > 0

    # Step 5: Check ascent/descent
    print(f"Trail {wikidata_qid} ascent: {trail.ascent:.1f} m, descent: {trail.descent:.1f} m")
    assert trail.ascent >= 0
    assert trail.descent >= 0
