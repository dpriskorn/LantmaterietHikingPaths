import logging
from pathlib import Path
import json
import requests
from typing import List
from pydantic import BaseModel
from requests.auth import HTTPBasicAuth
from pathlib import Path
import json
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

import config
from hiking_elevation.stac.collection import STACCollection
from tqdm import tqdm

CACHE_DIR = Path("data/stac_cache")
CACHE_DIR.mkdir(exist_ok=True, parents=True)
SAVE_DIR = Path("data/dem")
SAVE_DIR.mkdir(exist_ok=True, parents=True)

logger = logging.getLogger(__name__)


class STACOrchestrator(BaseModel):
    collections: List[STACCollection] = []

    class Config:
        arbitrary_types_allowed = True

    # ------------------------------
    # Collection management
    # ------------------------------
    def load_collections(self, force_refresh: bool = False):
        cache_file = CACHE_DIR / "collections.json"
        if cache_file.exists() and not force_refresh:
            logger.debug("Loading collection from cache")
            with open(cache_file) as f:
                data = json.load(f)
            self.collections = [STACCollection(**c) for c in data]
            print(f"Laddade {len(self.collections)} collection(s) från cache")
        else:
            url = "https://api.lantmateriet.se/stac-hojd/v1/collections"
            resp = requests.get(url)
            resp.raise_for_status()
            data = resp.json().get("collections", [])
            self.collections = [
                STACCollection(id=c["id"], title=c.get("title"), extent=c.get("extent"))
                for c in data
            ]
            with open(cache_file, "w") as f:
                json.dump([c.model_dump() for c in self.collections], f)
            print(f"Hämtade {len(self.collections)} collection(s) från API och cachade på disk")

    # ------------------------------
    # DEM tile management
    # ------------------------------
    @staticmethod
    def collection_intersects_bbox(collection: STACCollection, bbox: list) -> bool:
        spatial = collection.extent.get("spatial", {})
        bbox_c_list = spatial.get("bbox", [])
        if not bbox_c_list:
            return False
        mincx, mincy, maxcx, maxcy = bbox_c_list[0]
        minx, miny, maxx, maxy = bbox
        return not (maxx < mincx or minx > maxcx or maxy < mincy or miny > maxcy)

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30))
    def fetch_url(url: str):
        """Fetch a URL with retry/backoff."""
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def get_features_for_collection(collection_id: str) -> list:
        """
        Hämtar STAC items för en collection och cachar lokalt, med exponential backoff via tenacity.
        """
        cache_file = CACHE_DIR / f"{collection_id}_features.json"
        if cache_file.exists():
            with open(cache_file) as f:
                return json.load(f)

        features = []
        next_url = f"https://api.lantmateriet.se/stac-hojd/v1/collections/{collection_id}/items?limit=100"

        while next_url:
            data = STACOrchestrator.fetch_url(next_url)
            features.extend(data.get("features", []))

            # pagination
            next_url = None
            for link in data.get("links", []):
                if link.get("rel") == "next":
                    next_url = link["href"]
                    break

        with open(cache_file, "w") as f:
            json.dump(features, f)

        return features

    @staticmethod
    def download_dem_tile(url: str) -> Path:
        local_file = SAVE_DIR / url.split("/")[-1]
        if local_file.exists():
            return local_file

        # Streamed download with progress
        # Login to lantmäteriet to avoid 401 errors
        resp = requests.get(url, auth=HTTPBasicAuth(config.username, config.password), stream=True)
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        with open(local_file, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=local_file.name) as pbar:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                pbar.update(len(chunk))
        return local_file

    def get_dem_tiles_for_bbox(self, bbox: list) -> List[Path]:
        """
        Returnerar lokala Path till DEM-tiles som intersectar trailens bbox.
        Laddar ner nya tiles om de inte finns.
        """
        dem_paths = []
        for c in self.collections:
            # skip collections that don't touch bbox at all
            if not self.collection_intersects_bbox(c, bbox):
                continue

            # get all features for this collection
            features = self.get_features_for_collection(c.id)
            for feat in features:
                feat_bbox = feat.get("bbox")
                if not feat_bbox:
                    continue
                minfx, minfy, maxfx, maxfy = feat_bbox
                minx, miny, maxx, maxy = bbox

                # skip if this feature does NOT intersect trail bbox
                if maxx < minfx or minx > maxfx or maxy < minfy or miny > maxfy:
                    continue

                # get DEM asset
                assets = feat.get("assets", {})
                data_asset = assets.get("data")
                if data_asset and data_asset.get("type", "").startswith("image/tiff"):
                    url = data_asset["href"]
                    local_file = self.download_dem_tile(url)
                    dem_paths.append(local_file)

        return dem_paths
