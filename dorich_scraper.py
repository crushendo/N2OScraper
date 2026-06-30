#!/usr/bin/env python3
"""dorich_scraper.py

Read CSV files from DorichData/, clean them, write cleaned CSVs and save to a SQLite database.

Usage:
	python dorich_scraper.py --input DorichData --output DorichData/cleaned --db DorichData/dorich.db
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

try:
	from sqlalchemy import create_engine
except Exception:
	create_engine = None


def main():
	# Load CSV files from DorichData/
	site_library = pd.read_csv("DorichData/Sitelibrary_V1.csv")
	daily_data = pd.read_csv("DorichData/DailyGHG_V1.csv", encoding_errors='ignore')\
	
	# Print unique values of SiteID in daily_data for debugging
	print("Unique SiteIDs in daily_data:", daily_data["SiteID"].unique())
	
	
	# Create Treatment-level view dataframe from site_library
	site_library_clean = clean_site_library(site_library)
	# Get Sand, Silt, and Clay percentages from daily_data
	texture_df = get_texture(daily_data, site_library_clean)
	# Merge texture data into site_library_clean on ExperimentName and TreatmentName. Keep only
	# rows that have matches in site_library_clean
	site_library_clean = pd.merge(site_library_clean, texture_df, on=["SiteName", "TreatmentName"], how="left")
	
	# Delete duplicate rows if any
	site_library_clean = site_library_clean.drop_duplicates()
	
	# For each row in site_library_clean, identify the number of matching rows in daily_data
	# based on ExperimentName (SiteID) and TreatmentName (Treatment) where row "n2o" is not NA.
	# Add a new column "N2O_Measurements" to site_library_clean with this count.
	n2o_counts = []

	for _, site_row in site_library_clean.iterrows():
		site_name = site_row["ExperimentName"]
		treatment_name = site_row["TreatmentName"]

		matched_rows = daily_data[
			(daily_data["SiteID"] == site_name) &
			(daily_data["Treatment"] == treatment_name) &
			(daily_data["n2o"].notna())
		]
		n2o_counts.append(len(matched_rows))

	site_library_clean["N2O_Measurements"] = n2o_counts

	# If no n2o measurements exist, drop that row from site_library_clean
	site_library_clean = site_library_clean[site_library_clean["N2O_Measurements"] > 0]

	# Write cleaned CSVs to DorichData/cleaned/
	output_dir = Path("DorichData/cleaned")
	output_dir.mkdir(parents=True, exist_ok=True)

	site_library_clean.to_csv(output_dir / "site_library_cleaned.csv", index=False)


def get_texture(daily_data: pd.DataFrame, site_library_clean: pd.DataFrame) -> pd.DataFrame:
	# For each row in site_library_clean, find corresponding rows in daily_data
	# matched by SiteName (SiteID) and TreatmentName (Treatment). Then get the values for
	# Sand, Silt, and Clay (take the first non-NA value found).
	texture_list = []
	for _, site_row in site_library_clean.iterrows():
		site_name = site_row["ExperimentName"]
		treatment_name = site_row["TreatmentName"]
		
		matched_rows = daily_data[
			(daily_data["SiteID"] == site_name) &
			(daily_data["Treatment"] == treatment_name)
		]
		
		# If no matched rows, set sand, silt, clay to NaN
		if matched_rows.empty:
			sand = np.nan
			silt = np.nan
			clay = np.nan
			print(f"No matched rows for SiteID: {site_name}, Treatment: {treatment_name}")
		
		sand = matched_rows["Sand"].dropna().iloc[0] if not matched_rows["Sand"].dropna().empty else np.nan
		silt = matched_rows["Silt"].dropna().iloc[0] if not matched_rows["Silt"].dropna().empty else np.nan
		clay = matched_rows["Clay"].dropna().iloc[0] if not matched_rows["Clay"].dropna().empty else np.nan
		
		texture_list.append({
			"SiteName": site_row["SiteName"],
			"TreatmentName": treatment_name,
			"Sand": sand,
			"Silt": silt,
			"Clay": clay
		})
	texture_df = pd.DataFrame(texture_list)
	return texture_df

def clean_site_library(site_library: pd.DataFrame) -> pd.DataFrame:
	df = site_library.copy()
	# Keep already clean columns, rename (key: old name, value: new name)
	columns_to_keep = {
		"SiteID": "SiteName",
		"Treatment": "TreatmentName",
		"Latitude": "Latitude",
		"Longitude": "Longitude",
		"Reference": "ExperimentName",
		"Measurement_method": "FluxInstrument",
		"Treatment_Description": "TreatmentDescription"
	}

	# Select and rename columns
	experiment_view = df[list(columns_to_keep.keys())].rename(columns=columns_to_keep)

	# Add Citation column as PrimaryPaper or SecondaryPaper or Otherrepository/location
	# based on whether a value exists in those columns
	experiment_view["Citation"] = np.where(
		df["PrimaryPaper"].notna() & (df["PrimaryPaper"] != ""),
		df["PrimaryPaper"],
		np.where(
			df["SecondaryPaper"].notna() & (df["SecondaryPaper"] != ""),
			df["SecondaryPaper"],
			df["Other repository/location"]
		)
	)

	# Add a Management column based on "N type" and "Treatment_Description" columns
	# If "N type" == NA, set Management to "Zero Input"
	# If "N type" contains "urine" or "slurry", set Management to "Organic"
	# If "Treatment_Description" contains "no-till" or "no tillage", set Management to "No-till"
	# Else set Management to "Conventional"
	
	# First iterate through rows to determine Management
	management_list = []
	for _, row in df.iterrows():
		n_type = str(row.get("N type", "")).lower()
		treatment_desc = str(row.get("Treatment_Description", "")).lower()
		
		if pd.isna(row.get("N type")) or n_type == "":
			management_list.append("Zero Input")
		elif "urine" in n_type or "slurry" in n_type:
			management_list.append("Organic")
		elif "no-till" in treatment_desc or "no tillage" in treatment_desc:
			management_list.append("No-till")
		else:
			management_list.append("Conventional")
	experiment_view["Management"] = management_list

	return experiment_view

if __name__ == "__main__":
	raise SystemExit(main())
