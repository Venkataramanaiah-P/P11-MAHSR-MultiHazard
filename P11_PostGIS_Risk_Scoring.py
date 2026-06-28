# ============================================================
# P11 — MAHSR Corridor Multi-Hazard Risk Assessment
# Step 2: PostGIS Risk Scoring
# Author: Venkataramanaiah Poliboyina
# GitHub: github.com/Venkataramanaiah-P
# DB: urban_risk_db | CRS: EPSG:32643
# ============================================================

import psycopg2
import geopandas as gpd
from sqlalchemy import create_engine, text
import pandas as pd
import logging
import os

logging.basicConfig(level=logging.INFO)

# ── 1. CONNECTION ────────────────────────────────────────────
# Password read from environment variable PGPASSWORD_URBAN — set this
# before running, e.g. in PowerShell: $env:PGPASSWORD_URBAN="yourpassword"
engine = create_engine(
    f"postgresql://postgres:{os.getenv('PGPASSWORD_URBAN')}@localhost:5432/urban_risk_db"
)
conn = psycopg2.connect(
    dbname='urban_risk_db',
    user='postgres',
    password=os.getenv('PGPASSWORD_URBAN'),
    host='localhost',
    port=5432
)
cur = conn.cursor()
print('Connected to urban_risk_db')

# ── 2. CREATE SCHEMA ─────────────────────────────────────────
cur.execute("""
    CREATE SCHEMA IF NOT EXISTS p11_mahsr;
""")
conn.commit()
print('Schema p11_mahsr ready')

# ── 3. LOAD MAHSR CORRIDOR ───────────────────────────────────
# Corridor geometry — GeoJSON from GEE export / digitised 614-point line
mahsr_gdf = gpd.read_file(
    #r'D:\GIS_Projects\P11_MAHSR_Multihazard\data\raw\mahsr_corridor.geojson'
    r"D:\GIS_Projects\P10_MAHSR_Change_Detection\data\raw\mahsr_corridor.geojson"
)

# Reproject to EPSG:32643 if not already
if mahsr_gdf.crs is None or mahsr_gdf.crs.to_epsg() != 32643:
    mahsr_gdf = mahsr_gdf.to_crs(epsg=32643)

# Create 50km buffer polygon
mahsr_buffer = mahsr_gdf.copy()
mahsr_buffer['geometry'] = mahsr_gdf.geometry.buffer(50000)
mahsr_buffer = mahsr_buffer.dissolve()

# Push to PostGIS
mahsr_buffer.to_postgis(
    'corridor_buffer',
    engine,
    schema='p11_mahsr',
    if_exists='replace',
    index=False
)
print('Corridor buffer loaded → p11_mahsr.corridor_buffer')

# ── 4. LOAD SUPPORTING LAYERS (HOSPITALS + ROADS) ────────────
# Hospitals — reusing P4/P5 dataset (7,604 hospitals, Maharashtra)
# CONFIRMED FILE: hospitals_utm.gpkg in mh_road_project\data\processed\
# Format is GeoPackage (.gpkg), not Shapefile — filename indicates
# it's already in a UTM projection, but we verify/reproject defensively.
hospitals_gdf = gpd.read_file(
    r'D:\GIS_Projects\mh_road_project\data\processed\hospitals_utm.gpkg'
)
if hospitals_gdf.crs is None or hospitals_gdf.crs.to_epsg() != 32643:
    hospitals_gdf = hospitals_gdf.to_crs(epsg=32643)
hospitals_gdf.to_postgis(
    'hospitals_osm',
    engine,
    schema='p11_mahsr',
    if_exists='replace',
    index=False
)
print(f'Hospitals loaded: {len(hospitals_gdf)} features → p11_mahsr.hospitals_osm')

# Roads — found in Kolhapur_Flood project raw data (Geofabrik OSM
# western-zone extract, covers Maharashtra/Gujarat/Goa region).
# Folder: Kolhapur_Flood\data\raw\western-zone-260527-free.shp\
# File:   gis_osm_roads_free_1.shp (101-item Geofabrik extract)
roads_gdf = gpd.read_file(
    r'D:\GIS_Projects\Kolhapur_Flood\data\raw\western-zone-260527-free.shp\gis_osm_roads_free_1.shp'
)
if roads_gdf.crs is None or roads_gdf.crs.to_epsg() != 32643:
    roads_gdf = roads_gdf.to_crs(epsg=32643)

# Clip to corridor buffer extent — full western-zone file covers a much
# larger area (Maharashtra/Gujarat/Goa) than the 50km MAHSR corridor,
# so clip before loading to keep the PostGIS table small and fast.
roads_gdf = gpd.clip(roads_gdf, mahsr_buffer.to_crs(roads_gdf.crs))

roads_gdf.to_postgis(
    'roads_osm',
    engine,
    schema='p11_mahsr',
    if_exists='replace',
    index=False
)
print(f'Roads loaded: {len(roads_gdf)} features (clipped to corridor) → p11_mahsr.roads_osm')

# Spatial indexes + VACUUM ANALYZE on all loaded layers
cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_corridor_buffer_geom
        ON p11_mahsr.corridor_buffer USING GIST(geometry);
    CREATE INDEX IF NOT EXISTS idx_hospitals_geom
        ON p11_mahsr.hospitals_osm USING GIST(geometry);
    CREATE INDEX IF NOT EXISTS idx_roads_geom
        ON p11_mahsr.roads_osm USING GIST(geometry);
""")
conn.commit()

# VACUUM must run outside a transaction block — switch to autocommit temporarily
conn.autocommit = True
cur.execute("VACUUM ANALYZE p11_mahsr.corridor_buffer;")
cur.execute("VACUUM ANALYZE p11_mahsr.hospitals_osm;")
cur.execute("VACUUM ANALYZE p11_mahsr.roads_osm;")
conn.autocommit = False  # back to normal transactional mode for the rest of the script
print('Spatial indexes created + VACUUM ANALYZE complete')

# ── 5. CREATE GRID FOR RISK SCORING ─────────────────────────
# 5km × 5km grid over corridor buffer
conn.autocommit = True

cur.execute("""
    DROP TABLE IF EXISTS p11_mahsr.grid_5km;

    CREATE TABLE p11_mahsr.grid_5km AS
    WITH bounds AS (
        SELECT ST_Envelope(ST_Union(geometry)) AS geom
        FROM p11_mahsr.corridor_buffer
    ),
    grid AS (
        SELECT (ST_SquareGrid(5000, geom)).* FROM bounds
    )
    SELECT
        ROW_NUMBER() OVER () AS grid_id,
        geom AS geometry
    FROM grid g
    WHERE EXISTS (
        SELECT 1 FROM p11_mahsr.corridor_buffer cb
        WHERE ST_Intersects(g.geom, cb.geometry)
    );

    CREATE INDEX idx_grid_geom
        ON p11_mahsr.grid_5km USING GIST(geometry);
""")

# VACUUM must be its own separate execute() call — cannot be bundled
# with other SQL statements even when autocommit is True
cur.execute("VACUUM ANALYZE p11_mahsr.grid_5km;")

conn.autocommit = False
print('5km grid created over corridor')

# ── 6. RISK SCORING — SPATIAL SQL ───────────────────────────
# Score each grid cell based on:
# - Distance to nearest hospital (closer = lower risk)
# - Distance to nearest road (closer = lower risk)
# - Hospital count within 10km (more hospitals = lower risk)
conn.autocommit = True

cur.execute("""
    DROP TABLE IF EXISTS p11_mahsr.risk_scores;

    CREATE TABLE p11_mahsr.risk_scores AS
    WITH hospital_distance AS (
        SELECT
            g.grid_id,
            MIN(ST_Distance(g.geometry, h.geometry)) AS dist_hospital_m
        FROM p11_mahsr.grid_5km g
        LEFT JOIN p11_mahsr.hospitals_osm h
            ON ST_DWithin(g.geometry, h.geometry, 100000)
        GROUP BY g.grid_id
    ),
    hospital_count AS (
        SELECT
            g.grid_id,
            COUNT(*) AS hosp_count_10km
        FROM p11_mahsr.grid_5km g
        LEFT JOIN p11_mahsr.hospitals_osm h
            ON ST_DWithin(g.geometry, h.geometry, 10000)
        GROUP BY g.grid_id
    ),
    road_distance AS (
        SELECT
            g.grid_id,
            MIN(ST_Distance(g.geometry, r.geometry)) AS dist_road_m
        FROM p11_mahsr.grid_5km g
        LEFT JOIN p11_mahsr.roads_osm r
            ON ST_DWithin(g.geometry, r.geometry, 50000)
        GROUP BY g.grid_id
    ),
    raw_scores AS (
        SELECT
            g.grid_id,
            g.geometry,
            COALESCE(hd.dist_hospital_m, 100000) AS dist_hospital_m,
            COALESCE(hc.hosp_count_10km, 0)      AS hosp_count_10km,
            COALESCE(rd.dist_road_m, 50000)       AS dist_road_m
        FROM p11_mahsr.grid_5km g
        LEFT JOIN hospital_distance hd ON g.grid_id = hd.grid_id
        LEFT JOIN hospital_count    hc ON g.grid_id = hc.grid_id
        LEFT JOIN road_distance     rd ON g.grid_id = rd.grid_id
    ),
    normalized AS (
        SELECT
            grid_id,
            geometry,
            dist_hospital_m,
            hosp_count_10km,
            dist_road_m,
            -- Normalize each component to 0–1 (1 = highest risk)
            (dist_hospital_m - MIN(dist_hospital_m) OVER ()) /
                NULLIF(MAX(dist_hospital_m) OVER () - MIN(dist_hospital_m) OVER (), 0) AS norm_hosp_dist,
            (dist_road_m - MIN(dist_road_m) OVER ()) /
                NULLIF(MAX(dist_road_m) OVER () - MIN(dist_road_m) OVER (), 0) AS norm_road_dist,
            1 - ((hosp_count_10km - MIN(hosp_count_10km) OVER ()) /
                NULLIF(MAX(hosp_count_10km) OVER () - MIN(hosp_count_10km) OVER (), 0)::float) AS norm_hosp_scarcity
        FROM raw_scores
    )
    SELECT
        grid_id,
        geometry,
        dist_hospital_m,
        hosp_count_10km,
        dist_road_m,
        -- Weighted composite risk score (0–1, higher = higher risk)
        ROUND(
            (COALESCE(norm_hosp_dist, 0)     * 0.40 +
             COALESCE(norm_road_dist, 0)     * 0.30 +
             COALESCE(norm_hosp_scarcity, 0) * 0.30)::numeric, 4
        ) AS risk_score,
        CASE
            WHEN (COALESCE(norm_hosp_dist, 0) * 0.40 +
                  COALESCE(norm_road_dist, 0) * 0.30 +
                  COALESCE(norm_hosp_scarcity, 0) * 0.30) >= 0.80 THEN 'VERY HIGH'
            WHEN (COALESCE(norm_hosp_dist, 0) * 0.40 +
                  COALESCE(norm_road_dist, 0) * 0.30 +
                  COALESCE(norm_hosp_scarcity, 0) * 0.30) >= 0.60 THEN 'HIGH'
            WHEN (COALESCE(norm_hosp_dist, 0) * 0.40 +
                  COALESCE(norm_road_dist, 0) * 0.30 +
                  COALESCE(norm_hosp_scarcity, 0) * 0.30) >= 0.40 THEN 'MEDIUM'
            WHEN (COALESCE(norm_hosp_dist, 0) * 0.40 +
                  COALESCE(norm_road_dist, 0) * 0.30 +
                  COALESCE(norm_hosp_scarcity, 0) * 0.30) >= 0.20 THEN 'LOW'
            ELSE 'VERY LOW'
        END AS risk_class
    FROM normalized;

    CREATE INDEX idx_risk_scores_geom
        ON p11_mahsr.risk_scores USING GIST(geometry);

    
""")

cur.execute("VACUUM ANALYZE p11_mahsr.risk_scores;")

conn.autocommit = False  # back to normal transactional mode for the rest of the scriptprint('Risk scores calculated → p11_mahsr.risk_scores')

# ── 7. SUMMARY STATS WITH CTE + RANK ────────────────────────
summary_query = """
    WITH risk_summary AS (
        SELECT
            risk_class,
            COUNT(*)                        AS grid_cells,
            ROUND(AVG(risk_score)::numeric, 4) AS avg_risk_score,
            ROUND(MIN(dist_hospital_m)::numeric, 0) AS min_hosp_dist_m,
            ROUND(MAX(dist_hospital_m)::numeric, 0) AS max_hosp_dist_m,
            ROUND(AVG(hosp_count_10km)::numeric, 1) AS avg_hosp_10km
        FROM p11_mahsr.risk_scores
        GROUP BY risk_class
    )
    SELECT
        RANK() OVER (ORDER BY avg_risk_score DESC) AS rank,
        risk_class,
        grid_cells,
        avg_risk_score,
        min_hosp_dist_m,
        max_hosp_dist_m,
        avg_hosp_10km
    FROM risk_summary
    ORDER BY rank;
"""

summary_df = pd.read_sql(summary_query, engine)
print('\n── RISK SCORE SUMMARY ──────────────────────────────')
print(summary_df.to_string(index=False))

# ── 8. MATERIALISED VIEW FOR QGIS ───────────────────────────
cur.execute("""
    DROP MATERIALIZED VIEW IF EXISTS p11_mahsr.mv_risk_final;

    CREATE MATERIALIZED VIEW p11_mahsr.mv_risk_final AS
    SELECT
        grid_id,
        geometry,
        risk_score,
        risk_class,
        dist_hospital_m,
        hosp_count_10km,
        dist_road_m
    FROM p11_mahsr.risk_scores
    ORDER BY risk_score DESC;

    CREATE INDEX idx_mv_risk_geom
        ON p11_mahsr.mv_risk_final USING GIST(geometry);
""")
conn.commit()
print('Materialised view created → p11_mahsr.mv_risk_final')

# ── 9. EXPORT TO SHAPEFILE FOR QGIS ─────────────────────────
risk_gdf = gpd.read_postgis(
    "SELECT * FROM p11_mahsr.mv_risk_final",
    engine,
    geom_col='geometry'
)

output_path = r'D:\GIS_Projects\P11-\data\processed\P11_risk_scores.shp'
risk_gdf.to_file(output_path)
print(f'\nShapefile exported → {output_path}')
print(f'Total grid cells: {len(risk_gdf)}')
print(f'Risk class counts:\n{risk_gdf.risk_class.value_counts()}')

# ── 10. CLEANUP ──────────────────────────────────────────────
cur.close()
conn.close()
print('\nDone. Load P11_risk_scores.shp into QGIS for Step 3.')
print('Colour by risk_class: VERY HIGH=red, HIGH=orange,')
print('MEDIUM=yellow, LOW=lime, VERY LOW=green')