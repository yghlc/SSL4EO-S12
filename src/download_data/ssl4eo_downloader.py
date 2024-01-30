""" Sample and download Satellite tiles with Google Earth Engine

### run the script:

## Install and authenticate Google Earth Engine (https://developers.google.com/earth-engine/guides/python_install) # noqa: E501

## match and download pre-sampled locations
python ssl4eo_downloader.py \
    --save_path ./data \
    --collection COPERNICUS/S2 \
    --meta_cloud_name CLOUDY_PIXEL_PERCENTAGE \
    --cloud_pct 20 \
    --dates 2021-12-21 2021-09-22 2021-06-21 2021-03-20 \
    --radius 1320 \
    --bands B1 B2 B3 B4 B5 B6 B7 B8 B8A B9 B10 B11 B12 \
    --crops 44 264 264 264 132 132 132 264 132 44 44 132 132 \
    --dtype uint16 \
    --num_workers 8 \
    --log_freq 100 \
    --match_file ./data/sampled_locations.csv \
    --indices_range 0 250000

## resample and download new locations with rtree/grid overlap search
python ssl4eo_downloader.py \
    --save_path ./data \
    --collection COPERNICUS/S2 \
    --meta_cloud_name CLOUDY_PIXEL_PERCENTAGE \
    --cloud_pct 20 \
    --dates 2021-12-21 2021-09-22 2021-06-21 2021-03-20 \
    --radius 1320 \
    --bands B1 B2 B3 B4 B5 B6 B7 B8 B8A B9 B10 B11 B12 \
    --crops 44 264 264 264 132 132 132 264 132 44 44 132 132 \
    --dtype uint16 \
    --num_workers 8 \
    --log_freq 100 \
    --overlap_check rtree \
    --indices_range 0 250000

## resume from interruption (e.g. 20 ids processed)
python ssl4eo_downloader.py \
    -- ... \
    --resume ./data/checked_locations.csv \
    --indices_range 20 250000


## Example: download Landsat-8, match SSL4EO-S12 locations but keep same patch size
python ssl4eo_downloader.py \
    --save_path ./data \
    --collection LANDSAT/LC08/C02/T1_TOA \
    --meta_cloud_name CLOUD_COVER \
    --cloud_pct 20 \
    --dates 2021-12-21 2021-09-22 2021-06-21 2021-03-20 \
    --radius 1980 \
    --bands B1 B2 B3 B4 B5 B6 B7 B8 B9 B10 B11 \
    --crops 132 132 132 132 132 132 132 264 264 132 132 \
    --dtype float32 \
    --num_workers 8 \
    --log_freq 100 \
    --match_file ./data/ssl4eo-s12_center_coords.csv \
    --indices_range 0 250000

### Notes
# By default, the script will sample and download Sentinel-2 L1C tiles (13 bands) with cloud cover less than 20%. # noqa: E501
# The script will download 250k little-overlap locations, 4 tiles for each location, one for each season (in a two-year buffer). # noqa: E501
# You may want to extend the buffer to more years by modifying the `get_period()` and `filter_collection()` functions. # noqa: E501

"""

import argparse
import csv
import json
import math
import os
import time
import warnings
from collections import OrderedDict
from datetime import date, datetime, timedelta
from multiprocessing.dummy import Lock, Pool
from typing import Any, Dict, List, Optional, Tuple

import ee
import numpy as np
import rasterio
import shapefile
import geopandas as gpd
import pandas as pd
from pyproj import CRS
import urllib3
from rasterio.transform import Affine
from rtree import index
from shapely.geometry import Point, shape
# from torchvision.datasets.utils import download_and_extract_archive
from tqdm import tqdm

warnings.simplefilter("ignore", UserWarning)


""" samplers to get locations of interest points"""


class UniformSampler:
    def sample_point(self) -> List[float]:
        lon = np.random.uniform(-180, 180)
        lat = np.random.uniform(-90, 90)
        return [lon, lat]


class GaussianSampler:
    def __init__(
        self,
        interest_points: Optional[List[List[float]]] = None,
        input_vector: Optional[str] = None,
        num_cities: int = 1000,
        std: float = 20,
    ) -> None:
        if interest_points is None:
            if os.path.isfile(input_vector) is False:
                cities = self.get_world_cities()
                self.interest_points = self.get_interest_points(cities, size=num_cities)
            else:
                # read point from input_vector
                self.interest_points = self.get_interest_points_from_vectorfile(input_vector)
        else:
            self.interest_points = interest_points
        self.std = std

    def sample_point(self) -> List[float]:
        rng = np.random.default_rng()
        point = rng.choice(self.interest_points)
        std = self.km2deg(self.std)
        lon, lat = np.random.normal(loc=point, scale=[std, std])
        return [lon, lat]

    @staticmethod
    def get_world_cities(download_root: str = "world_cities") -> List[Dict[str, Any]]:
        url = "https://simplemaps.com/static/data/world-cities/basic/simplemaps_worldcities_basicv1.71.zip"  # noqa: E501
        filename = "worldcities.csv"
        if not os.path.exists(os.path.join(download_root, os.path.basename(url))):
            download_and_extract_archive(url, download_root)
        with open(os.path.join(download_root, filename), encoding="UTF-8") as csvfile:
            reader = csv.DictReader(csvfile, delimiter=",", quotechar='"')
            cities = []
            for row in reader:
                row["population"] = (
                    row["population"].replace(".", "") if row["population"] else "0"
                )
                cities.append(row)
        return cities

    @staticmethod
    def get_interest_points(
        cities: List[Dict[str, str]], size: int = 10000
    ) -> List[List[float]]:
        cities = sorted(cities, key=lambda c: int(c["population"]), reverse=True)[:size]
        points = [[float(c["lng"]), float(c["lat"])] for c in cities]
        return points

    @staticmethod
    def get_interest_points_from_vectorfile(
        vector_path: str
    ) -> List[List[float]]:
        gdf = gpd.read_file(vector_path)

        # Check the current projection
        current_crs = gdf.crs
        target_crs = CRS.from_epsg(4326)
        # Reproject if necessary
        if current_crs != target_crs:
            gdf = gdf.to_crs(target_crs)

        if gdf.geom_type[0] != 'Point':
            raise ValueError("The geometry type is not Point.")

        # Extract the latitude and longitude coordinates
        latitudes = gdf.geometry.y
        longitudes = gdf.geometry.x

        # points = [[float(c["lng"]), float(c["lat"])] for c in cities]
        points = [[lng, lat ] for lat, lng in zip(latitudes, longitudes)]

        # save to copy to csv
        save_csv_path = os.path.splitext(os.path.basename(vector_path))[0] + '_latlon.csv'
        data = pd.DataFrame({'Latitude': latitudes, 'Longitude': longitudes})
        # Save the DataFrame to a CSV file
        data.to_csv(save_csv_path, index=False)


        return points

    @staticmethod
    def km2deg(kms: float, radius: float = 6371) -> float:
        return kms / (2.0 * radius * np.pi / 360.0)

    @staticmethod
    def deg2km(deg: float, radius: float = 6371) -> float:
        return deg * (2.0 * radius * np.pi / 360.0)


class BoundedUniformSampler:
    def __init__(self, boundaries: shape = None) -> None:
        if boundaries is None:
            self.boundaries = self.get_country_boundaries()
        else:
            self.boundaries = boundaries

    def sample_point(self) -> List[float]:
        minx, miny, maxx, maxy = self.boundaries.bounds
        lon = np.random.uniform(minx, maxx)
        lat = np.random.uniform(miny, maxy)
        p = Point(lon, lat)
        if self.boundaries.contains(p):
            return [p.x, p.y]
        else:
            return self.sample_point()

    @staticmethod
    def get_country_boundaries(
        download_root: str = os.path.expanduser("~/.cache/naturalearth"),
    ) -> shape:
        url = "https://www.naturalearthdata.com/http//www.naturalearthdata.com/download/110m/cultural/ne_110m_admin_0_countries.zip"  # noqa: E501
        filename = "ne_110m_admin_0_countries.shp"
        if not os.path.exists(os.path.join(download_root, os.path.basename(url))):
            download_and_extract_archive(url, download_root)
        sf = shapefile.Reader(os.path.join(download_root, filename))
        return shape(sf.shapes().__geo_interface__)


class OverlapError(Exception):
    pass


def date2str(date: datetime) -> str:
    return date.strftime("%Y-%m-%d")


def get_period(date: datetime, days: int = 5) -> Tuple[str, str, str, str]:
    date1 = date - timedelta(days=days / 2)
    date2 = date + timedelta(days=days / 2)
    date3 = date1 - timedelta(days=365)
    date4 = date2 - timedelta(days=365)
    return (
        date2str(date1),
        date2str(date2),
        date2str(date3),
        date2str(date4),
    )  # two-years buffer


"""get collection and remove clouds from ee"""


def maskS2clouds(args: Any, image: ee.Image) -> ee.Image:
    qa = image.select(args.qa_band)
    cloudBitMask = 1 << args.qa_cloud_bit
    # Both flags should be set to zero, indicating clear conditions.
    mask = qa.bitwiseAnd(cloudBitMask).eq(0)
    return image.updateMask(mask)


def get_collection(
    collection_name: str, meta_cloud_name: str, cloud_pct: float
) -> ee.ImageCollection:
    collection = ee.ImageCollection(collection_name)
    collection = collection.filter(ee.Filter.lt(meta_cloud_name, cloud_pct))
    # Uncomment the following line if you want to apply cloud masking.
    # collection = collection.map(maskS2clouds, args)
    return collection


def filter_collection(
    collection: ee.ImageCollection,
    coords: List[float],
    period: Tuple[str, str, str, str],
) -> ee.ImageCollection:
    filtered = collection
    if period is not None:
        # filtered = filtered.filterDate(*period)  # filter time, if there's one period
        filtered = filtered.filter(
            ee.Filter.Or(
                ee.Filter.date(period[0], period[1]),
                ee.Filter.date(period[2], period[3]),
            )
        )  # filter time, if there're two periods

    filtered = filtered.filterBounds(ee.Geometry.Point(coords))  # filter region

    if filtered.size().getInfo() == 0:
        raise ee.EEException(
            f"ImageCollection.filter: No suitable images found in ({coords[1]:.4f}, {coords[0]:.4f}) between {period[0]} and {period[1]}."  # noqa: E501
        )
    return filtered


def center_crop(
    img: np.ndarray[Any, np.dtype[Any]], out_size: Tuple[int, int]
) -> np.ndarray[Any, np.dtype[Any]]:
    image_height, image_width = img.shape[:2]
    crop_height, crop_width = out_size
    crop_top = (image_height - crop_height + 1) // 2
    crop_left = (image_width - crop_width + 1) // 2
    return img[crop_top : crop_top + crop_height, crop_left : crop_left + crop_width]


def adjust_coords(
    coords: List[List[float]], old_size: Tuple[int, int], new_size: Tuple[int, int]
) -> List[List[float]]:
    xres = (coords[1][0] - coords[0][0]) / old_size[1]
    yres = (coords[0][1] - coords[1][1]) / old_size[0]
    xoff = int((old_size[1] - new_size[1] + 1) * 0.5)
    yoff = int((old_size[0] - new_size[0] + 1) * 0.5)
    return [
        [coords[0][0] + (xoff * xres), coords[0][1] - (yoff * yres)],
        [
            coords[0][0] + ((xoff + new_size[1]) * xres),
            coords[0][1] - ((yoff + new_size[0]) * yres),
        ],
    ]


def get_properties(image: ee.Image) -> Any:
    return image.getInfo()


def get_patch(
    collection: ee.ImageCollection,
    center_coord: List[float],
    radius: float,
    bands: List[str],
    crop: Optional[Dict[str, Any]] = None,
    dtype: str = "float32",
) -> Dict[str, Any]:
    image = collection.sort("system:time_start", False).first()  # get most recent
    region = (
        ee.Geometry.Point(center_coord).buffer(radius).bounds()
    )  # sample region bound
    patch = image.select(*bands).sampleRectangle(region, defaultValue=0)

    features = patch.getInfo()  # the actual download

    raster = OrderedDict()
    for band in bands:
        img = np.atleast_3d(features["properties"][band])
        if crop is not None:
            img = center_crop(img, out_size=crop[band])
        raster[band] = img.astype(dtype)

    coords0 = np.array(features["geometry"]["coordinates"][0])
    coords = [
        [coords0[:, 0].min(), coords0[:, 1].max()],
        [coords0[:, 0].max(), coords0[:, 1].min()],
    ]
    if crop is not None:
        band = bands[0]
        old_size = (
            len(features["properties"][band]),
            len(features["properties"][band][0]),
        )
        new_size = raster[band].shape[:2]
        coords = adjust_coords(coords, old_size, new_size)

    return OrderedDict(
        {"raster": raster, "coords": coords, "metadata": get_properties(image)}
    )


""" get data --- match from pre-sampled locations """


def get_random_patches_match(
    idx: int,
    collection: ee.ImageCollection,
    bands: List[str],
    crops: Dict[str, Any],
    dtype: str,
    dates: List[Any],
    radius: float,
    debug: bool = False,
    match_coords: Dict[str, Any] = {},
) -> Tuple[Optional[List[Dict[str, Any]]], List[float]]:
    # (lon,lat) of idx patch
    coords = match_coords[str(idx)]

    # random +- 30 days of random days within 1 year from the reference dates
    periods = [get_period(date, days=60) for date in dates]

    try:
        filtered_collections = [
            filter_collection(collection, coords, p) for p in periods
        ]
        patches = [
            get_patch(c, coords, radius, bands=bands, crop=crops, dtype=dtype)
            for c in filtered_collections
        ]

    except (ee.EEException, urllib3.exceptions.HTTPError) as e:
        if debug:
            print(e)
        return None, coords

    return patches, coords


""" sample new coord, check overlap, and get data --- rtree """


def get_random_patches_rtree(
    idx: int,
    collection: ee.ImageCollection,
    bands: List[str],
    crops: Dict[str, Any],
    dtype: str,
    sampler: GaussianSampler,
    dates: List[Any],
    radius: float,
    debug: bool = False,
    rtree_obj: index.Index = None,
) -> Tuple[List[Dict[str, Any]], List[float]]:
    # (lon,lat) of top-10000 cities
    coords = sampler.sample_point()

    # use rtree to avoid strong overlap
    try:
        new_coord = (coords[0], coords[1])
        for i in rtree_obj.nearest(new_coord, num_results=1, objects=True):
            distance = np.sqrt(
                sampler.deg2km(abs(new_coord[0] - i.bbox[2])) ** 2
                + sampler.deg2km(abs(new_coord[1] - i.bbox[3])) ** 2
            )
            if distance < (1.5 * radius / 1000):
                raise OverlapError
        rtree_obj.insert(
            len(rtree_obj) - 1, (new_coord[0], new_coord[1], new_coord[0], new_coord[1])
        )

    except OverlapError:
        patches, center_coord = get_random_patches_rtree(
            idx,
            collection,
            bands,
            crops,
            dtype,
            sampler,
            dates,
            radius,
            debug,
            rtree_obj,
        )

    # random +- 30 days of random days within 1 year from the reference dates
    periods = [get_period(date, days=60) for date in dates]

    try:
        filtered_collections = [
            filter_collection(collection, coords, p) for p in periods
        ]
        patches = [
            get_patch(c, coords, radius, bands=bands, crop=crops, dtype=dtype)
            for c in filtered_collections
        ]
        center_coord = coords

    except (ee.EEException, urllib3.exceptions.HTTPError) as e:
        if debug:
            print(e)
        rtree_obj.insert(
            len(rtree_obj) - 1, (new_coord[0], new_coord[1], new_coord[0], new_coord[1])
        )  # prevent from sampling an old coord that doesn't fit the collection
        patches, center_coord = get_random_patches_rtree(
            idx,
            collection,
            bands,
            crops,
            dtype,
            sampler,
            dates,
            radius,
            debug,
            rtree_obj,
        )

    return patches, center_coord


""" sample new coord, check overlap, and get data --- grid """


def get_random_patches_grid(
    idx: int,
    collection: ee.ImageCollection,
    bands: List[str],
    crops: Dict[str, Any],
    dtype: str,
    sampler: GaussianSampler,
    dates: List[Any],
    radius: float,
    debug: bool = False,
    grid_dict: Dict[Tuple[int, int], Any] = {},
) -> Tuple[List[Dict[str, Any]], List[float]]:
    # (lon,lat) of top-10000 cities
    coords = sampler.sample_point()

    # avoid strong overlap
    try:
        new_coord = (coords[0], coords[1])
        gridIndex = (math.floor(new_coord[0] + 180), math.floor(new_coord[1] + 90))

        if gridIndex not in grid_dict.keys():
            grid_dict[gridIndex] = {new_coord}
        else:
            for coord in grid_dict[gridIndex]:
                distance = np.sqrt(
                    sampler.deg2km(abs(new_coord[0] - coord[0])) ** 2
                    + sampler.deg2km(abs(new_coord[1] - coord[1])) ** 2
                )
                if distance < (1.5 * radius / 1000):
                    raise OverlapError
            grid_dict[gridIndex].add(new_coord)

    except OverlapError:
        patches, center_coord = get_random_patches_grid(
            idx,
            collection,
            bands,
            crops,
            dtype,
            sampler,
            dates,
            radius,
            debug,
            grid_dict=grid_dict,
        )

    # random +- 15 days of random days within 1 year from the reference dates
    periods = [get_period(date, days=30) for date in dates]

    try:
        filtered_collections = [
            filter_collection(collection, coords, p) for p in periods
        ]
        patches = [
            get_patch(c, coords, radius, bands=bands, crop=crops, dtype=dtype)
            for c in filtered_collections
        ]

        center_coord = coords

    except (ee.EEException, urllib3.exceptions.HTTPError) as e:
        if debug:
            print(e)
        patches, center_coord = get_random_patches_grid(
            idx,
            collection,
            bands,
            crops,
            dtype,
            sampler,
            dates,
            radius,
            debug,
            grid_dict=grid_dict,
        )

    return patches, center_coord


def save_geotiff(
    img: np.ndarray[Any, np.dtype[Any]], coords: List[List[float]], filename: str
) -> None:
    height, width, channels = img.shape
    xres = (coords[1][0] - coords[0][0]) / width
    yres = (coords[0][1] - coords[1][1]) / height
    transform = Affine.translation(
        coords[0][0] - xres / 2, coords[0][1] + yres / 2
    ) * Affine.scale(xres, -yres)
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": channels,
        "crs": "+proj=latlong",
        "transform": transform,
        "dtype": img.dtype,
        "compress": "lzw",
        "predictor": 2,
    }
    with rasterio.open(filename, "w", **profile) as f:
        f.write(img.transpose(2, 0, 1))


def save_patch(
    raster: Dict[str, Any],
    coords: List[List[float]],
    metadata: Dict[str, Any],
    path: str,
) -> None:
    patch_id = metadata["properties"]["system:index"]
    patch_path = os.path.join(path, patch_id)
    os.makedirs(patch_path, exist_ok=True)

    for band, img in raster.items():
        save_geotiff(img, coords, os.path.join(patch_path, f"{band}.tif"))

    with open(os.path.join(patch_path, "metadata.json"), "w") as f:
        json.dump(metadata, f)


class Counter:
    def __init__(self, start: int = 0) -> None:
        self.value = start
        self.lock = Lock()

    def update(self, delta: int = 1) -> int:
        with self.lock:
            self.value += delta
            return self.value


def fix_random_seeds(seed: int = 42) -> None:
    np.random.seed(seed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save_path", type=str, default="./data/", help="dir to save data"
    )
    parser.add_argument(
        "--input_vector", type=str, default="", help="a vector file contain points"
    )
    # collection properties
    parser.add_argument(
        "--collection", type=str, default="COPERNICUS/S2", help="GEE collection name"
    )
    parser.add_argument(
        "--qa_band", type=str, default="QA60", help="qa band name (optional)"
    )  # optional
    parser.add_argument(
        "--qa_cloud_bit", type=int, default=10, help="qa band cloud bit (optional)"
    )  # optional
    parser.add_argument(
        "--meta_cloud_name",
        type=str,
        default="CLOUDY_PIXEL_PERCENTAGE",
        help="meta data cloud percentage name",
    )
    parser.add_argument(
        "--cloud_pct", type=int, default=20, help="cloud percentage threshold"
    )
    # patch properties
    parser.add_argument(
        "--dates",
        type=str,
        nargs="+",
        default=["2021-12-21", "2021-09-22", "2021-06-21", "2021-03-20"],
        help="reference dates",
    )
    parser.add_argument(
        "--radius", type=int, default=1320, help="patch radius in meters"
    )
    parser.add_argument(
        "--bands",
        type=str,
        nargs="+",
        default=[
            "B1",
            "B2",
            "B3",
            "B4",
            "B5",
            "B6",
            "B7",
            "B8",
            "B8A",
            "B9",
            "B10",
            "B11",
            "B12",
        ],
        help="bands to download",
    )
    parser.add_argument(
        "--crops",
        type=int,
        nargs="+",
        default=[44, 264, 264, 264, 132, 132, 132, 264, 132, 44, 44, 132, 132],
        help="crop size for each band",
    )
    parser.add_argument("--dtype", type=str, default="float32", help="data type")
    # sampler properties
    parser.add_argument(
        "--num_cities", type=int, default=10000, help="number of cities to sample"
    )
    parser.add_argument(
        "--std", type=int, default=50, help="std of gaussian distribution"
    )
    # download settings
    parser.add_argument("--num_workers", type=int, default=8, help="number of workers")
    parser.add_argument("--log_freq", type=int, default=10, help="print frequency")
    parser.add_argument(
        "--resume", type=str, default=None, help="resume from a previous run"
    )
    # sampler options
    # op1: match pre-sampled coordinates and indexes
    parser.add_argument(
        "--match_file",
        type=str,
        default=None,
        help="match pre-sampled coordinates and indexes",
    )
    # op2-3: resample from scratch, grid or rtree based overlap check
    parser.add_argument(
        "--overlap_check",
        type=str,
        default="rtree",
        choices=["grid", "rtree", None],
        help="overlap check method",
    )
    # number of locations to download
    parser.add_argument(
        "--indices_range",
        type=int,
        nargs=2,
        default=[0, 250000],
        help="indices to download",
    )
    # debug
    parser.add_argument("--debug", action="store_true", help="debug mode")

    args = parser.parse_args()

    fix_random_seeds(seed=42)

    # initialize ee
    ee.Initialize()

    # get data collection (remove clouds)
    collection = get_collection(args.collection, args.meta_cloud_name, args.cloud_pct)

    # initialize sampler
    sampler = GaussianSampler(input_vector=args.input_vector, num_cities=args.num_cities, std=args.std)

    dates = []
    for d in args.dates:
        dates.append(date.fromisoformat(d))

    bands = args.bands
    crops = {}
    for i, band in enumerate(bands):
        crops[band] = (args.crops[i], args.crops[i])
    dtype = args.dtype

    # if resume
    ext_coords = {}
    ext_flags = {}
    if args.resume:
        ext_path = args.resume
        with open(ext_path) as csv_file:
            reader = csv.reader(csv_file)
            for row in reader:
                key = row[0]
                val1 = float(row[1])
                val2 = float(row[2])
                ext_coords[key] = (val1, val2)  # lon, lat
                ext_flags[key] = int(row[3])  # success or not
    else:
        ext_path = os.path.join(args.save_path, "checked_locations.csv")

    # if match from pre-sampled coords (e.g. SSL4EO-S12)
    if args.match_file:
        match_coords = {}
        with open(args.match_file) as csv_file:
            reader = csv.reader(csv_file)
            for row in reader:
                key = row[0]
                val1 = float(row[1])
                val2 = float(row[2])
                match_coords[key] = (val1, val2)  # lon, lat
    # else need to check overlap
    # build grid or rtree from existing coordinates
    elif args.overlap_check is not None:
        grid_dict: Dict[Any, Any] = {}
        rtree_coords = index.Index()
        if args.resume:
            print("Load existing locations.")
            for i, key in enumerate(tqdm(ext_coords.keys())):
                c = ext_coords[key]
                rtree_coords.insert(i, (c[0], c[1], c[0], c[1]))
                gridIndex = (math.floor(c[0] + 180), math.floor(c[1] + 90))
                if gridIndex not in grid_dict.keys():
                    grid_dict[gridIndex] = {c}
                else:
                    grid_dict[gridIndex].add(c)
    else:
        raise NotImplementedError

    start_time = time.time()
    counter = Counter()

    def worker(idx: int) -> None:
        if str(idx) in ext_coords.keys():
            if args.match_file:  # skip all processed ids
                return
            else:
                if ext_flags[str(idx)] != 0:  # only skip downloaded ids
                    return

        if args.match_file:
            patches, center_coord = get_random_patches_match(
                idx,
                collection,
                bands,
                crops,
                dtype,
                dates,
                radius=args.radius,
                debug=args.debug,
                match_coords=match_coords,
            )
        elif args.overlap_check == "rtree":
            patches, center_coord = get_random_patches_rtree(
                idx,
                collection,
                bands,
                crops,
                dtype,
                sampler,
                dates,
                radius=args.radius,
                debug=args.debug,
                rtree_obj=rtree_coords,
            )
        elif args.overlap_check == "grid":
            patches, center_coord = get_random_patches_grid(
                idx,
                collection,
                bands,
                crops,
                dtype,
                sampler,
                dates,
                radius=args.radius,
                debug=args.debug,
                grid_dict=grid_dict,
            )
        else:
            raise NotImplementedError

        if patches is not None:
            if args.save_path is not None:
                # s2c
                location_path = os.path.join(args.save_path, "imgs", f"{idx:06d}")
                os.makedirs(location_path, exist_ok=True)
                for patch in patches:
                    save_patch(
                        raster=patch["raster"],
                        coords=patch["coords"],
                        metadata=patch["metadata"],
                        path=location_path,
                    )

            count = counter.update(1)
            if count % args.log_freq == 0:
                print(f"Downloaded {count} images in {time.time() - start_time:.3f}s.")
        else:
            print("no suitable image for location %d." % (idx))

        # add to existing checked locations
        with open(ext_path, "a") as f:
            writer = csv.writer(f)
            if patches is not None:
                if args.match_file:
                    success = 2
                else:
                    success = 1
            else:
                success = 0
            data = [idx, center_coord[0], center_coord[1], success]
            writer.writerow(data)

        return

    # set indices
    if args.match_file is not None:
        indices = []
        for key in match_coords.keys():
            indices.append(int(key))
        indices = indices[args.indices_range[0] : args.indices_range[1]]
    elif args.indices_range is not None:
        indices = list(range(args.indices_range[0], args.indices_range[1]))
    else:
        print("Please set up indices.")
        raise NotImplementedError

    if args.num_workers == 0:
        for i in indices:
            worker(i)
    else:
        # parallelism data
        with Pool(processes=args.num_workers) as p:
            p.map(worker, indices)
