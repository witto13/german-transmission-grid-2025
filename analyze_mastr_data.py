#!/usr/bin/env python3
"""
Analyze MaStR (Marktstammdatenregister) power-plant registry data.

Purpose
-------
Loads the pre-downloaded MaStR CSV exports for wind, solar, and conventional
power plants and produces a consolidated technology-by-status breakdown table.
This gives a quick overview of the German generation fleet that is available
for the 2025 target year, including unit counts and installed capacity.

Algorithm / Method
------------------
1. Reads three CSV files from ``data/mastr/``:
   - ``wind_2025_all.csv`` -- all wind turbines
   - ``solar_2025_all.csv`` -- all PV installations
   - ``conventional_2025_all.csv`` -- thermal and other conventional plants
2. For conventional plants, further splits by fuel type (gas, coal,
   lignite, oil, biomass, etc.) using the ``fuel_type`` column.
3. Groups each technology by operational status (``InBetrieb``,
   ``Stillgelegt``, etc.) and computes unit count and total capacity in GW
   (converted from the ``capacity_mw`` column).
4. Appends row-wise totals per status across all technologies.
5. Prints a formatted summary table to stdout and saves it to CSV.

Inputs
------
- ``data/mastr/wind_2025_all.csv`` -- Wind MaStR export (~2.4 MB)
- ``data/mastr/solar_2025_all.csv`` -- Solar MaStR export (~288 MB)
- ``data/mastr/conventional_2025_all.csv`` -- Conventional MaStR export (~5 MB)

Each CSV must contain at least the columns: ``status``, ``capacity_mw``.

Outputs
-------
- ``mastr_overview.csv`` -- Technology x Status summary table with columns:
  Technology, Status, Units, Capacity_GW.
- Formatted table printed to stdout.

Usage
-----
::

    conda activate egon2025
    python analyze_mastr_data.py
"""

import pandas as pd
import numpy as np

def analyze_mastr_data():
    """Load and analyze all MaStR CSV files."""

    # Load wind data
    print("Loading wind data...")
    wind = pd.read_csv('data/mastr/wind_2025_all.csv')
    wind['technology'] = 'Wind'

    # Load solar data
    print("Loading solar data...")
    solar = pd.read_csv('data/mastr/solar_2025_all.csv')
    solar['technology'] = 'Solar'

    # Load conventional data
    print("Loading conventional data...")
    conventional = pd.read_csv('data/mastr/conventional_2025_all.csv')

    # For conventional, create technology breakdown by fuel type
    if 'fuel_type' in conventional.columns:
        conventional['technology'] = conventional['fuel_type'].fillna('Unknown')
    else:
        conventional['technology'] = 'Conventional'

    # Combine all data
    all_data = []

    # Process wind
    wind_summary = create_summary(wind, 'Wind')
    all_data.extend(wind_summary)

    # Process solar
    solar_summary = create_summary(solar, 'Solar')
    all_data.extend(solar_summary)

    # Process conventional by fuel type
    for fuel_type in conventional['technology'].unique():
        fuel_data = conventional[conventional['technology'] == fuel_type]
        fuel_summary = create_summary(fuel_data, f'Conventional - {fuel_type}')
        all_data.extend(fuel_summary)

    # Create summary DataFrame
    df = pd.DataFrame(all_data)

    # Add totals
    totals = []
    for status in df['Status'].unique():
        status_data = df[df['Status'] == status]
        totals.append({
            'Technology': 'TOTAL',
            'Status': status,
            'Units': status_data['Units'].sum(),
            'Capacity_GW': status_data['Capacity_GW'].sum()
        })

    df_with_totals = pd.concat([df, pd.DataFrame(totals)], ignore_index=True)

    return df_with_totals

def create_summary(data, tech_name):
    """Create summary for a technology."""
    summaries = []

    # Group by status
    for status in data['status'].unique():
        status_data = data[data['status'] == status]
        summaries.append({
            'Technology': tech_name,
            'Status': status,
            'Units': len(status_data),
            'Capacity_GW': status_data['capacity_mw'].sum() / 1000
        })

    return summaries

if __name__ == '__main__':
    df = analyze_mastr_data()

    # Sort by technology and status
    df = df.sort_values(['Technology', 'Status'])

    # Print as table
    print("\n" + "="*80)
    print("MaStR Data Overview - 2025")
    print("="*80)
    print(df.to_string(index=False))
    print("="*80)

    # Save to CSV
    df.to_csv('mastr_overview.csv', index=False)
    print("\nSaved to: mastr_overview.csv")
