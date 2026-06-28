# P11 — MAHSR Multi-Hazard Risk Assessment

## Overview
Multi-hazard risk assessment for the Mumbai–Ahmedabad High Speed Rail (MAHSR) 
Corridor C3-SEC3 (Boisar–Bhilad), covering a 45km stretch of the 508km corridor.

## Pipeline
- **Step 1 — GEE:** LST, NDVI, NDWI exported at 10m resolution (EPSG:32643)
- **Step 2 — PostGIS:** Risk scoring using spatial SQL (558 cells)
- **Step 3 — QGIS:** Weighted overlay (NDWI 35%, PostGIS 30%, LST 20%, NDVI 15%)
- **Step 4 — ArcGIS Pro:** Cartographic layout export

## Key Numbers
- Corridor: 508 km total | Package: 156 km (L&T C3) | Stretch: 45 km
- Resolution: 10 metres | CRS: EPSG:32643
- Risk Range: 0.36 – 0.52

## Tools
GEE Python API · PostGIS · QGIS 3.40 · ArcGIS Pro · Python 3.12

Note: Large raster files (LST, NDVI, NDWI, Final TIF) not included due to file size. Available on request.

## Author
Venkataramanaiah Poliboyina | Survey Manager, L&T MAHSR
