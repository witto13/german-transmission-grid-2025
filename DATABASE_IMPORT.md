# Database Import Instructions

## Overview

The database dump (`german_grid_v3_database.tar.gz`, ~104 MB) contains the grid topology data for scenarios eGon2025 (v1), eGon2025v2, and eGon2025v3. It is available as a GitHub Release asset.

## Contents

The archive contains:
- `01_schema.sql` -- Schema definitions (grid + scenario schemas)
- `egon_etrago_bus.csv` -- Bus data (v1: 14,494 + v2: 8,925 + v3: 8,753 = 32,172 rows)
- `egon_etrago_line.csv` -- Line data (v1 + v2 + v3)
- `egon_etrago_transformer.csv` -- Transformer data (535 per scenario)
- `egon_etrago_generator.csv` -- Generator data (24,972 in eGon2025)
- `egon_etrago_load.csv` -- Load data (7,256 in eGon2025)
- Supporting tables (substations, bus metadata, carrier definitions, etc.)
- `02_boundaries_data.sql` -- Geographic boundaries (municipalities, NUTS regions)

## Quick Setup

### 1. Start PostgreSQL Container

```bash
docker run -d --name egon-data-local-database-container \
  -p 59734:5432 \
  -e POSTGRES_USER=egon \
  -e POSTGRES_PASSWORD=data \
  -e POSTGRES_DB=egon-data \
  postgis/postgis:16-3
```

Wait a few seconds for PostgreSQL to initialize.

### 2. Extract the Dump

```bash
mkdir -p db_import
cd db_import
tar xzf ../german_grid_v3_database.tar.gz
```

### 3. Create Schemas and Tables

```bash
docker exec -i -e PGPASSWORD=data egon-data-local-database-container \
  psql -U egon -d egon-data < 01_schema.sql
```

### 4. Import CSV Data

```bash
# Import each CSV into its corresponding table
for TABLE in egon_etrago_bus egon_etrago_line egon_etrago_transformer \
             egon_etrago_generator egon_etrago_load egon_etrago_storage \
             egon_etrago_store egon_etrago_link egon_etrago_carrier \
             egon_etrago_temp_resolution egon_hvmv_substation \
             egon_ehv_substation egon_hvmv_transfer_buses \
             egon_ehv_transfer_buses egon_bus_metadata plz_to_bus_mapping; do

  if [ -f "${TABLE}.csv" ] && [ -s "${TABLE}.csv" ]; then
    echo "Importing grid.${TABLE}..."
    docker exec -i -e PGPASSWORD=data egon-data-local-database-container \
      psql -U egon -d egon-data \
      -c "\COPY grid.${TABLE} FROM STDIN WITH CSV HEADER" < "${TABLE}.csv"
  fi
done
```

### 5. Import Boundaries (Optional)

```bash
docker exec -i -e PGPASSWORD=data egon-data-local-database-container \
  psql -U egon -d egon-data < 02_boundaries_data.sql
```

### 6. Verify Import

```bash
docker exec -e PGPASSWORD=data egon-data-local-database-container \
  psql -U egon -d egon-data -c "
    SELECT scn_name, COUNT(*) as buses
    FROM grid.egon_etrago_bus
    GROUP BY scn_name ORDER BY scn_name;"
```

Expected output:
```
  scn_name  | buses
------------+-------
 eGon2025   | 14494
 eGon2025v2 |  8925
 eGon2025v3 |  8753
```

## Connection Details

| Parameter | Value |
|-----------|-------|
| Host | `127.0.0.1` |
| Port | `59734` |
| Database | `egon-data` |
| User | `egon` |
| Password | `data` |

```python
from sqlalchemy import create_engine
engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
```
