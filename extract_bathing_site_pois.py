#!/bin/python
import argparse
from zipfile import ZipFile

import geopandas as gpd
from pyproj import CRS

# Create the argument parser
parser = argparse.ArgumentParser(description="Convert GeoPackage coordinates from SWEREF99TM to WGS84 and export to GeoJSON")

# Add the input and output file arguments
parser.add_argument("-i", "--input", help="Path to the input Zip file")
parser.add_argument("-o", "--output", help="Path to the output GeoJSON file")

# Parse the command-line arguments
args = parser.parse_args()

# Input and output file paths
input_zip = args.input
output_geojson = args.output

with ZipFile(input_zip) as myzip:
    print("Reading zip")
    with myzip.open(input_zip.split("/")[-1].replace("zip", "gpkg")) as mygpkg:
        print("Reading gpkg")

        layer_name = "anlaggningsomradespunkt"

        # Read the GeoPackage into a GeoDataFrame
        gdf = gpd.read_file(input_zip, layer=layer_name)

        # Filter the GeoDataFrame based on the "objekttypnr" attribute
        filtered_gdf = gdf[(gdf["objekttypnr"] == 2843) & (gdf["andamal"].str.lower() == "badplats")]

        # Define the target coordinate reference system (CRS)
        target_crs = CRS.from_epsg(4326)  # WGS84

        # Set the CRS attribute on the GeoDataFrame
        filtered_gdf.crs = CRS.from_epsg(3006)  # SWEREF99TM

        # Transform the coordinates to the target CRS
        transformed_gdf = filtered_gdf.to_crs(crs=target_crs)

        # Print the transformed GeoDataFrame
        print(transformed_gdf)

        # Save the filtered features to a new GeoJSON file
        print("Writing geojson")
        transformed_gdf.to_file(output_geojson, driver="GeoJSON")
