import logging
from pathlib import Path

from pydantic import BaseModel
from wikibaseintegrator import wbi_config  # type: ignore
from wikibaseintegrator.wbi_helpers import execute_sparql_query

import config
from hiking_elevation.stac.orchestrator import STACOrchestrator
from hiking_elevation.trail import Trail

logging.basicConfig(level=config.loglevel)
logger = logging.getLogger(__name__)


class HikingPipeline(BaseModel):
    """
    Full pipeline för svenska leder:
    1) Förbereda STAC
    2) Ladda svenska leder (från Wikidata)
    3) Loop för varje trail:
       a) hämta geometri
       b) hämta DEM tiles
       c) beräkna ascent/descent
       d) ladda upp resultat
    """
    trails: list[Trail] = []
    dem_dir: Path = Path("data/dem")
    orchestrator: STACOrchestrator | None = None
    sparql_result: dict = dict()
    rdf_entity_prefix: str = "http://www.wikidata.org/entity/"

    class Config:
        arbitrary_types_allowed = True

    # ------------------------------
    # STAC preparation
    # ------------------------------
    def prepare_stac(self, force_refresh: bool = False):
        logger.debug("prepare_stac: running")
        self.orchestrator = STACOrchestrator()
        self.orchestrator.load_collections(force_refresh=force_refresh)

    # ------------------------------
    # Load Swedish trails
    # ------------------------------
    @staticmethod
    def setup_wbi():
        wbi_config.config["USER_AGENT"] = config.user_agent

    def get_and_parse_trails(self):
        self.setup_wbi()
        self.__get_sparql_result__()
        self.__extract_items_from_sparql__()

    def __get_sparql_result__(self):
        """Get all hiking trails and subtrails in the specified country and
        with labels in the specified language"""
        logger.debug("prepare_stac: running")
        # minus discontinued hiking paths
        # only swedish paths
        # only those with osm id
        # only those with length and start with the shortest
        self.sparql_result = execute_sparql_query(
            f"""
            SELECT DISTINCT ?item ?osm ?length WHERE {{
              ?item wdt:P6104 wd:Q119978050;
                    wdt:P17 wd:Q34;
                    wdt:P402 ?osm;
                    wdt:P2043 ?length.
              MINUS {{ ?item wdt:P31 wd:Q116787033 }}
              # filter for length between 5 and 10 km
              FILTER(?length > 10 && ?length < 25)              
            }}
            ORDER BY ASC(?length)
            """
        )

    def __extract_wcdqs_json_entity_id__(
        self, data: dict, sparql_variable: str = "item"
    ) -> str:
        """We default to "item" as sparql value because
        it is customary in the Wikibase ecosystem"""
        return str(data[sparql_variable]["value"].replace(self.rdf_entity_prefix, ""))

    def __extract_items_from_sparql__(self) -> None:
        """Create TrailItem objects from SPARQL results including last_update"""
        if self.sparql_result:
            for binding in self.sparql_result["results"]["bindings"]:
                # pprint(binding)
                qid = self.__extract_wcdqs_json_entity_id__(data=binding)
                osm_relation_id = binding.get("osm", {}).get("value")
                logger.debug(f"got osm relation id: {osm_relation_id}")
                length = binding.get("length", {}).get("value")
                logger.debug(f"got length: {length} km")
                trail_item = Trail(wikidata_qid=qid, osm_relation_id=osm_relation_id, length=length)
                # pprint(trail_item)
                # input("press enter to cont")
                self.trails.append(trail_item)
        print(f"Got {len(self.trails)} trails from WDQS")

    # ------------------------------
    # Process a single trail
    # ------------------------------
    def process_trail(self, trail: Trail, sample_distance: float = 25.0):
        print(f"Processing trail ({trail.wikidata_qid}, {trail.length} km, {trail.wikidata_url})...")

        # 1) hämta geometri från OSM
        if trail.geometry is None:
            trail.fetch_geometry()

        # 2) hämta DEM tiles via orchestrator
        bbox = list(trail.geometry.bounds)
        logger.debug(f"bbox: {bbox}")
        dem_tiles = self.orchestrator.get_dem_tiles_for_bbox(bbox)

        if not dem_tiles:
            print(f"Inga DEM tiles hittades för {trail.wikidata_url}")
            return

        # 3) beräkna elevations, ascent/descent
        trail.sample_elevations(dem_tiles, sample_distance)

        # 4) ladda upp resultat till Wikidata
        # self.upload_to_wikidata(trail)

        # print(f"Done: Ascent={trail.ascent:.1f} m, Descent={trail.descent:.1f} m")

    # ------------------------------
    # Run pipeline
    # ------------------------------
    def run(self, sample_distance: float = 25.0):
        self.prepare_stac()
        self.get_and_parse_trails()
        for trail in self.trails:
            if trail.ascent == 0.0 and trail.descent == 0.0:
                self.process_trail(trail, sample_distance)
