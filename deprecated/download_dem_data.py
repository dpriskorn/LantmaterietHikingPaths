#!/usr/bin/env python3
import requests
from pathlib import Path

from requests.auth import HTTPBasicAuth
from tqdm import tqdm

import config

COLLECTION_ID = "mhm-62_4"
SAVE_DIR = Path("../data/dem")
SAVE_DIR.mkdir(exist_ok=True, parents=True)
LIMIT = 50  # antal items per request (kan justeras)

def get_features(collection_id: str, limit: int = 50):
    """
    Hämta alla features från STAC collection med pagination
    """
    features = []
    next_url = f"https://api.lantmateriet.se/stac-hojd/v1/collections/{collection_id}/items?limit={limit}"
    while next_url:
        resp = requests.get(next_url)
        resp.raise_for_status()
        data = resp.json()
        features.extend(data.get("features", []))
        # Pagination: kolla om "links" har rel="next"
        next_url = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_url = link["href"]
                break
    return features


def download_file(url: str, save_dir: Path):
    filename = save_dir / url.split("/")[-1]
    # HEAD request för filstorlek
    head = requests.head(url)
    size = int(head.headers.get("Content-Length", 0))
    size_mb = size / (1024*1024)
    print(f"Laddar ner {filename.name} ({size_mb:.2f} MB)...")

    # Streamad nedladdning med progress
    # Lantmäteriet requires login for download to work
    r = requests.get(url, auth=HTTPBasicAuth(config.username, config.password), stream=True)
    r.raise_for_status()
    with open(filename, "wb") as f, tqdm(total=size, unit='B', unit_scale=True, desc=filename.name) as pbar:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            pbar.update(len(chunk))
    return filename


def main():
    print(f"Hämtar features för collection {COLLECTION_ID}...")
    features = get_features(COLLECTION_ID)
    print(f"Totalt {len(features)} features hittade.")

    downloaded_files = []
    for feature in features:
        assets = feature.get("assets", {})
        data_asset = assets.get("data")
        if data_asset and data_asset.get("type", "").startswith("image/tiff"):
            url = data_asset["href"]
            try:
                f = download_file(url, SAVE_DIR)
                downloaded_files.append(f)
            except Exception as e:
                print(f"Misslyckades med att ladda ner {url}: {e}")

    print(f"Nedladdade {len(downloaded_files)} DEM-filer till {SAVE_DIR}")


if __name__ == "__main__":
    main()
