import numpy as np
import pandas as pd

# Load the CSV
df = pd.read_csv("DorichData/cleaned/daily_data_with_treatment_ids.csv")

# if wfps and vwc > 1, divide by 100
df['WFPS'] = df['WFPS'].apply(lambda x: x / 100 if x > 1 else x)
df['VWC'] = df['VWC'].apply(lambda x: x / 100 if x > 1 else x)

# Save the modified DataFrame back to CSV
df.to_csv("DorichData/cleaned/daily_data_with_treatment_ids.csv", index=False)

