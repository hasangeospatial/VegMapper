#!/usr/bin/env python

import os
import geopandas as gpd
from osgeo import gdal
import rasterio
from rasterio.crs import CRS
import subprocess
from concurrent.futures import ThreadPoolExecutor

def map_burst2tile(reference_tiles, burst_summary_gdf, rtc_dir):
    """
    This function reads the reference tiles
    and creates a geodataframe that will be 
    used to map the burst into their 
    corresponding tile. 
    Inputs:
        reference_tiles - Geojson
        burst_summary_gdf - Geodataframe
    
    """
    
    # Load the GeoJSON file into a GeoDataFrame
    tile_gdf = gpd.read_file(reference_tiles)
    burst_gdf = burst_summary_gdf.copy()
    # extract target crs from tiles
    tile_crs = tile_gdf.crs
    tile_epsg_number = tile_crs.to_string().split(':')[-1]
    # set burst crs
    burst_gdf.set_crs(epsg=4326, inplace=True) # This epsg is hard coded because its expected to be always the same.
    # Ensure both GeoDataFrames have the same CRS before overlapping
    tile_gdf = tile_gdf.to_crs(epsg=tile_epsg_number)
    burst_gdf = burst_gdf.to_crs(epsg=tile_epsg_number)
    
    # Perform spatial join to find overlapping geometries
    overlapping_gdf = gpd.sjoin(burst_gdf, tile_gdf, how="inner", predicate="intersects")
    
    # Group by the index of geojson_gdf to collect overlapping 'names'
    overlap_dict = overlapping_gdf.groupby('index_right')['burst_id'].apply(list).to_dict()
    
    # Add the overlapping names back to the GeoJSON GeoDataFrame
    tile_gdf['overlapping_bursts'] = tile_gdf.index.map(overlap_dict)

    # Export geodataframe as geojson
    def join_lists(cell):
        if isinstance(cell, list):
            return ','.join(cell)
        return cell

    gdf2export = tile_gdf.copy()
    # Convert the list of strings in 'overlapping_names' to a single string
    gdf2export['overlapping_bursts'] = gdf2export['overlapping_bursts'].apply(join_lists)
    # Convert geometry to WKT format for CSV export
    gdf2export['geometry'] = gdf2export['geometry'].apply(lambda x: x.wkt)
    # Export to CSV
    gdf2export.to_csv(f"{rtc_dir}/burst_to_tile_map.csv", index=False)

    return tile_gdf


def get_epsg(file_path):
    with rasterio.open(file_path) as dataset:
        crs = dataset.crs
        if crs:
            return crs.to_epsg()
        return None
        

def process_row(row, polarizations, rtc_dir, out_vrt_dir, target_crs, created_files):
    if row['mask'] == 0:
        return

    overlapping_names = row['overlapping_bursts']
    bbox = row['geometry'].bounds  # [minx, miny, maxx, maxy]
    position_h = row['h']
    position_v = row['v']
    target_epsg = CRS.from_string(target_crs).to_epsg()

    for pol in polarizations:
        files_2_merge = [f"{rtc_dir}/{name}_tmean_{pol}.tif" for name in overlapping_names]
        files_2_merge = [file for file in files_2_merge if os.path.exists(file)]
        
        output_tif = f'{out_vrt_dir}/tile_h{str(position_h)}_v{str(position_v)}_{pol}.tif'
        output_vrt_mosaic = f'{out_vrt_dir}/mosaic_h{str(position_h)}_v{str(position_v)}_{pol}.vrt'

        reprojected_files = []
        for file in files_2_merge:
            reprojected_file = file.replace('.tif', '_reprojected.vrt')

            if reprojected_file not in created_files:
                created_files.add(reprojected_file)
                if not os.path.exists(reprojected_file):
                    # Use a temporary file to ensure atomic creation
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.vrt') as temp_file:
                        temp_path = temp_file.name
                    try:
                        warp_command_reproject = [
                            'gdalwarp', '-q',
                            '-t_srs', target_crs,
                            '-r', 'near',
                            '-dstnodata', 'nan',
                            '-of', 'VRT',
                            file, temp_path
                        ]
                        subprocess.run(warp_command_reproject, check=True)
                        os.rename(temp_path, reprojected_file)
                    except Exception as e:
                        # Cleanup in case of error
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        raise e
            
            reprojected_files.append(reprojected_file)

        # Build VRT mosaic
        buildvrt_command = ['gdalbuildvrt', '-q', '-srcnodata', 'nan', '-vrtnodata', 'nan', output_vrt_mosaic] + reprojected_files
        subprocess.run(buildvrt_command, check=True)

        # Create final GeoTIFF
        warp_command = [
            'gdalwarp',
            '-overwrite',
            '-t_srs', target_crs, '-et', '0',
            '-te', str(bbox[0]), str(bbox[1]), str(bbox[2]), str(bbox[3]),
            '-srcnodata', 'nan', '-dstnodata', 'nan',
            '-r', 'near',
            '-co', 'COMPRESS=LZW',
            '-of', 'GTiff',
            output_vrt_mosaic,
            output_tif,
        ]
        subprocess.run(warp_command, check=True)

def build_opera_vrt(burst2tile_gdf, rtc_dir):
    # Output directory
    out_vrt_dir = f'{rtc_dir}/tile_vrts'
    os.makedirs(out_vrt_dir, exist_ok=True)

    polarizations = ['VV', 'VH']
    target_crs = burst2tile_gdf.crs.to_string()

    # Shared set for tracking created files
    created_files = set()

    # Process rows in parallel
    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(process_row, row, polarizations, rtc_dir, out_vrt_dir, target_crs, created_files)
            for _, row in burst2tile_gdf.iterrows()
        ]
        # Wait for all tasks to complete
        for future in futures:
            future.result()


def check_tiles_exist(gdf, path):
    """
    Check if VV and VH tile files exist for each row in the GeoDataFrame.

    Parameters:
    - gdf: GeoDataFrame with columns 'h', 'v', and 'mask'.
    - path: Path to the directory containing the tile files.

    Returns:
    - A dictionary with a tuple (h, v) as the key and a dictionary indicating the existence of VV and VH tiles.
    """
    results = {}
    all_exist = True  # Flag to track if all required tiles exist

    for _, row in gdf.iterrows():
        h, v, mask = row['h'], row['v'], row['mask']
        
        # Tile names for VV and VH
        vv_tile_name = f"tile_h{h}_v{v}_VV.tif"
        vh_tile_name = f"tile_h{h}_v{v}_VH.tif"
        
        vv_file_path = os.path.join(path, vv_tile_name)
        vh_file_path = os.path.join(path, vh_tile_name)

        if mask == 1:
            # Check existence of both VV and VH tiles
            vv_exists = os.path.exists(vv_file_path)
            vh_exists = os.path.exists(vh_file_path)
            
            # Store results for both tile types
            results[(h, v)] = {'VV': vv_exists, 'VH': vh_exists}

            # Update flag if any tile is missing
            if not (vv_exists and vh_exists):
                all_exist = False
        else:
            # Indicate that no tiles are expected
            results[(h, v)] = {'VV': None, 'VH': None}

    # Print message if all required tiles exist
    if all_exist:
        tile_confirmation = 'All tiles exist'
        print(tile_confirmation)
    else:
        tile_confirmation = 'Tiles not found'
        print(tile_confirmation)

    return tile_confirmation


def create_vrt_mosaic(path):
    """
    Create a VRT mosaic of all tiles in the specified directory.

    Parameters:
    - path: Directory to search for tile files.

    Returns:
    - None. Creates the VRT file at the specified location.
    """
    # Collect all .tif files ending in *VV.tif
    vv_tile_files = [os.path.join(path, f) for f in os.listdir(path) if f.endswith("VV.tif")]

    if not vv_tile_files:
        print("No tiles found ending in *VV.tif. Mosaic not created.")
        return
    
    # Build VRT mosaic
    output_vv_vrt = f'{path}/tile_mosaic_VV.vrt' 
    vrt_options = gdal.BuildVRTOptions(resampleAlg="nearest")  # Use nearest-neighbor resampling
    gdal.BuildVRT(output_vv_vrt, vv_tile_files, options=vrt_options)
    print(f"VRT mosaic created successfully at: {output_vv_vrt}")

    # Collect all .tif files ending in *VV.tif
    vh_tile_files = [os.path.join(path, f) for f in os.listdir(path) if f.endswith("VH.tif")]

    if not vh_tile_files:
        print("No tiles found ending in *VH.tif. Mosaic not created.")
        return
    
    # Build VRT mosaic
    output_vh_vrt = f'{path}/tile_mosaic_VH.vrt' 
    vrt_options = gdal.BuildVRTOptions(resampleAlg="nearest")  # Use nearest-neighbor resampling
    gdal.BuildVRT(output_vh_vrt, vh_tile_files, options=vrt_options)
    print(f"VRT mosaic created successfully at: {output_vh_vrt}")
