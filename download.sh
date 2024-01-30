#!/bin/bash
set -eE -o functrace


python ./src/download_data/ssl4eo_downloader.py \
    --save_path ./save_data \
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