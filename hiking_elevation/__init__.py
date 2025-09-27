import csv
import logging
import time
from pathlib import Path
from typing import Set

from pydantic import BaseModel, Field
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
       d) spara resultat
    """
    trails: list[Trail] = Field(default_factory=list)
    processed_qids: Set[str] = Field(default_factory=set)
    no_tiles_qids: Set[str] = Field(default_factory=set)
    dem_dir: Path = Path("data/dem")
    orchestrator: STACOrchestrator | None = None
    sparql_result: dict = Field(default_factory=dict)
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
        self.sparql_result = execute_sparql_query(
            f"""
            SELECT DISTINCT ?item ?osm ?length WHERE {{
              ?item wdt:P6104 wd:Q119978050;
                    wdt:P17 wd:Q34;
                    wdt:P402 ?osm;
                    wdt:P2043 ?length.
              MINUS {{ ?item wdt:P31 wd:Q116787033 }}
              FILTER(?length > 5 && ?length < 50)              
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
                qid = self.__extract_wcdqs_json_entity_id__(data=binding)
                osm_relation_id = binding.get("osm", {}).get("value")
                logger.debug(f"got osm relation id: {osm_relation_id}")
                length = binding.get("length", {}).get("value")
                logger.debug(f"got length: {length} km")
                trail_item = Trail(wikidata_qid=qid, osm_relation_id=osm_relation_id, length=length)
                self.trails.append(trail_item)
        print(f"Got {len(self.trails)} trails from WDQS")

    # ------------------------------
    # Process a single trail
    # ------------------------------
    def process_trail(self, trail: Trail):
        # 1) hämta geometri från OSM
        if trail.geometry is None:
            start = time.time()
            trail.fetch_geometry()
            elapsed = time.time() - start
            logger.info(f"Step 1: fetch geometry took {elapsed:.1f} seconds")

        if trail.geometry is not None:
            # 2) hämta DEM tiles via orchestrator
            start = time.time()
            bbox = list(trail.geometry.bounds)
            logger.debug(f"bbox: {bbox}")
            dem_tiles = self.orchestrator.get_dem_tiles_for_bbox(bbox)
            elapsed = time.time() - start
            logger.info(f"Step 2: fetch DEM tiles took {elapsed:.1f} seconds")

            if not dem_tiles:
                print(f"Inga DEM tiles hittades för {trail.wikidata_url}")
                self.append_no_tiles_qid(trail.wikidata_qid)
                return

            # 3) beräkna elevations, ascent/descent
            start = time.time()
            trail.sample_elevations(dem_tiles)
            elapsed = time.time() - start
            logger.info(f"Step 3: sample elevations took {elapsed:.1f} seconds")

            # 4) beräkna stigning
            trail.compute_ascent_descent()

            # 5) spara resultat
            trail.save_to_csv()

    def load_processed_qids(self):
        """
        Läser CSV-filen specificerad i config.output_csv_path
        och laddar första kolumnen (QID) till self.processed_qids.
        """
        csv_path = config.output_csv_path
        if not csv_path.exists():
            print(f"CSV-fil finns inte: {csv_path}")
            return

        with csv_path.open(newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if row:
                    self.processed_qids.add(row[0])

    def load_no_tiles_qids(self):
        """
        Läser CSV-filen specificerad i config.no_tiles_csv_path
        och laddar första kolumnen (QID) till self.no_tiles_qids.
        """
        csv_path = config.no_tiles_csv_path
        if not csv_path.exists():
            logger.info(f"No-tiles CSV not found: {csv_path}")
            return

        with csv_path.open(newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if row:
                    self.no_tiles_qids.add(row[0])

    def append_no_tiles_qid(self, qid: str):
        """
        Lägger till en QID i no_tiles_csv_path om den inte redan finns.
        """
        csv_path = config.no_tiles_csv_path
        if qid not in self.no_tiles_qids:
            with csv_path.open("a", newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([qid])
            logger.info(f"Appended {qid} to {csv_path}")

    # ------------------------------
    # Run pipeline
    # ------------------------------
    def run(self):
        self.load_processed_qids()
        self.load_no_tiles_qids()
        self.prepare_stac()
        self.get_and_parse_trails()
        count = 1
        excluded_trails = self.processed_qids.union(self.no_tiles_qids)
        for trail in self.trails:
            if trail.wikidata_qid not in excluded_trails:
                print(f"Processing trail ({count}/{len(self.trails)}) "
                      f"({trail.wikidata_qid}, {trail.length} km, {trail.wikidata_url})...")
                self.process_trail(trail)
            else:
                if trail.wikidata_qid in self.processed_qids:
                    print(f"Skipping already processed QID: {trail.wikidata_qid}")
                if trail.wikidata_qid in self.no_tiles_qids:
                    print(f"Skipping no tiles QID: {trail.wikidata_qid}")
            count += 1
