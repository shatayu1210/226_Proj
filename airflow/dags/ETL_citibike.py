# Importing necessary modules
import os
import zipfile
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
import requests

from airflow import DAG
from airflow.models import Variable
from airflow.models import DagRun
from airflow.decorators import task
from airflow.providers.http.hooks.http import HttpHook
from airflow.operators.dagrun_operator import TriggerDagRunOperator
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

import snowflake.connector



# Set up a directory for storing/accessing intermediate files
LOCAL_STORAGE_DIR = '/opt/airflow/temp_store_proj'


def return_snowflake_conn():
    hook = SnowflakeHook(snowflake_conn_id='snowflake_conn') # Initialize the SnowflakeHook
    conn = hook.get_conn()
    return conn, conn.cursor() # Returning connection (to close it once database operations are done) as well as cursor object (to operate on databases)
 

@task
def extract_citibike_data(start_year, start_month, end_year, end_month):
    """
    Extract Citibike trip data within the range of passed start/end year/month and combine into a single DataFrame.
    Save this dataframe as an intermediate dataframe to avoid processing overhead, since it consists of over 3M records.
    
    Args:
        start_year (int): The start year (e.g., 2020).
        end_year (int): The end year (e.g., 2024).
    """
    base_url = "https://s3.amazonaws.com/tripdata/"
    all_data = []  # List to store DataFrames for each file
    print(f"Fetching citibike ride data from {start_year}-{start_month} to {end_year}-{end_month}")
    
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):  # Iterate through all months
            if (year == start_year and month < start_month) or (year == end_year and month > end_month):
                continue  # Skip months outside the range
            # Construct the file name and URL
            year_month = f"{year}{month:02d}"
            if year_month == "202411":
                print(f"Reached {year_month}, exiting extraction.")
                return pd.concat(all_data, ignore_index=True) if all_data else None  # Exit early when reaching 202411
            elif year_month == "202207":
                zip_file_name = f"JC-{year_month}-citbike-tripdata.csv.zip"  # Special case for 202207, naming mistake on S3 by source
            else:
                zip_file_name = f"JC-{year_month}-citibike-tripdata.csv.zip"
            
            url = base_url + zip_file_name

            try:
                # Ensure the our extract destination exists
                os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)
                
                # Step 1: Fetch the ZIP file via HTTP
                response = requests.get(url)
                response.raise_for_status()  # Raise HTTPError for bad responses (4xx and 5xx)

                # Step 2: Open the ZIP file
                zip_file = zipfile.ZipFile(BytesIO(response.content))
                if year_month == "202207":
                    csv_file_name = f"JC-{year_month}-citbike-tripdata.csv"  # Special case for 202207, naming mistake on S3 by source
                else:
                    csv_file_name = f"JC-{year_month}-citibike-tripdata.csv" # Consistent naming for other dated dumps

                # Step 3: Extract and read the CSV from ZIP
                with zip_file.open(csv_file_name) as my_file:
                    df = pd.read_csv(my_file)
                    all_data.append(df)  # Append DataFrame to the list
                print(f"Successfully processed: {csv_file_name}")
            
            # Catch any exceptions that may arise during extraction phase
            except Exception as e:
                print(f"Failed to process {zip_file_name} during extraction: {e}")
                continue

    # Step 4: Combine all CSV DataFrames into one
    if all_data:
        combined_data = pd.concat(all_data, ignore_index=True)

        # Step 5: To avoid processing overhead save the combined DataFrame as a local CSV file for transform stage
        combined_data_file = os.path.join(LOCAL_STORAGE_DIR, 'extracted_data.csv')
        combined_data.to_csv(combined_data_file, index=False)
        return combined_data_file
    else:
        print("No data extracted for given range.")
        return None


@task
def transform_citibike_data(extracted_file_path):
    """Transform the extracted data by:
        1. Adding new columns: start_year, start_month, start_day, duration.
        2. Dropping records having any missing column.
        3. Removing records that don't have a unique ride_id.
    
    Args: extracted_file_path (string): Path indicating location of extract output CSV, to perform
    transformations on.
    """
    try:
        # Declaring output path for this function (store the transformed data)
        output_file = os.path.join(LOCAL_STORAGE_DIR, 'cleaned_data.csv')
        
        # Load extracted file as a data frame for performing transformations
        extracted_df = pd.read_csv(extracted_file_path)
        print("Extracted Rows: ", len(extracted_df))

        # Add '.000000' to timestamps that don't have fractional seconds, to keep the structure consistent and
        # prevent errors while deriving duration column
        extracted_df['started_at'] = extracted_df['started_at'].apply(
            lambda x: x + '.000000' if '.' not in x else x
        )
        extracted_df['ended_at'] = extracted_df['ended_at'].apply(
            lambda x: x + '.000000' if '.' not in x else x
        )
        
        # Now that all values are consistent, convert 'started_at' and 'ended_at' to datetime format
        extracted_df['started_at'] = pd.to_datetime(extracted_df['started_at'], format='%Y-%m-%d %H:%M:%S.%f')
        extracted_df['ended_at'] = pd.to_datetime(extracted_df['ended_at'], format='%Y-%m-%d %H:%M:%S.%f')

        # Step 1: Populate 'start_year', 'start_month', 'start_day' columns by splitting stated_at timestamp
        extracted_df['start_year'] = extracted_df['started_at'].dt.year
        extracted_df['start_month'] = extracted_df['started_at'].dt.month
        extracted_df['start_day'] = extracted_df['started_at'].dt.day

        # Step 2: Populate a calculated 'duration' in minutes and seconds (decimal) for easy aggregation during ELT
        extracted_df['duration'] = (extracted_df['ended_at'] - extracted_df['started_at']).dt.total_seconds() / 60  # Duration in minutes
        extracted_df['duration'] = extracted_df['duration'].apply(lambda x: round(x, 2))  # Rounding to 2 decimal places
        
        # Step 3: Drop rows with any null values
        df_cleaned = extracted_df.dropna()
        print("Row count after dropping records with any null attributes: ", len(df_cleaned))
        
        # Step 4: Group by 'ride_id' and count the number of rows in each group
        ride_id_count = df_cleaned.groupby('ride_id').size().reset_index(name='count')
        
        # Step 5: Filter out groups where the count of rows is greater than 1
        valid_rides = ride_id_count[ride_id_count['count'] == 1]
        
        # Step 6: Merge back with the original cleaned data to get rows with unique 'ride_id'
        df_deduped = pd.merge(df_cleaned, valid_rides[['ride_id']], on='ride_id', how='inner')
        print("Row count after dropping duplicates: ", len(df_deduped))
        
        # Step 7: Save cleaned data to CSV as local intermediate source file for load task
        df_deduped.to_csv(output_file, index=False)
        print(f"\nSaved transformed data to: {output_file}")
        print("\nFeatures in transformed data: ", df_deduped.columns)
        print("Sample records: \n", df_deduped.head())
    
    # Catch any exceptions that may arise during transformations        
    except Exception as e:
        print(f"An error occurred during citibike data transformation: {e}")
  
    
@task
def load_citibike_data(table, cleaned_file_path):
    """Load the cleaned data to Snowflake with transaction."""
    try:
        conn, cursor = return_snowflake_conn()  # Initialize Snowflake connection and cursor
        cursor.execute("BEGIN;")  # Start the SQL transaction (Idempotency)

        cleaned_df = pd.read_csv(cleaned_file_path)
        print("Row count for data to be pushed: ", len(cleaned_df))
        
        cursor.execute("USE DATABASE DEV;")
        cursor.execute("USE SCHEMA RAW_DATA;")

        create_file_format_sql = """CREATE OR REPLACE FILE FORMAT my_csv_format
            TYPE = 'CSV'
            FIELD_OPTIONALLY_ENCLOSED_BY = '"'
            FIELD_DELIMITER = ','
            SKIP_HEADER = 1
            DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'
            ENCODING = 'UTF8';
            """
        # Execute the SQL command to create the file format
        cursor.execute(create_file_format_sql)
        print("File format created successfully.")    
        
        
        # Step 1: Create stage (if not already exists)
        create_stage_sql = """
            CREATE OR REPLACE STAGE dev.raw_data.my_csv_rides_stage
                FILE_FORMAT = (FORMAT_NAME = my_csv_format);
        """
        cursor.execute(create_stage_sql)  # Execute stage creation for loading CSV data
        print("Stage created successfully.") # Acknowledging
        
        # Step 2: Upload the local CSV file to the Snowflake stage
        put_sql = f"""
        PUT file://{cleaned_file_path} @dev.raw_data.my_csv_rides_stage;
        """
        cursor.execute(put_sql)
        print(f"File uploaded to stage: {cleaned_file_path}") # Acknowledging
        
        # Step 3: Create or replace table
        create_table_sql = f"""
            CREATE OR REPLACE TABLE {table} (
            ride_id STRING PRIMARY KEY,            -- Alphanumeric and primary key
            rideable_type STRING,                  -- String type for rideable type
            started_at TIMESTAMP_NTZ(9),           -- Timestamp with fractional seconds (up to 9 digits of precision)
            ended_at TIMESTAMP_NTZ(9),             -- Timestamp with fractional seconds (up to 9 digits of precision)
            start_station_name STRING,             -- String for station name
            start_station_id STRING,               -- Alphanumeric for start station ID
            end_station_name STRING,               -- String for end station name
            end_station_id STRING,                 -- Alphanumeric for end station ID
            start_lat FLOAT,                       -- Latitude as a float
            start_lng FLOAT,                       -- Longitude as a float
            end_lat FLOAT,                         -- Latitude as a float
            end_lng FLOAT,                         -- Longitude as a float
            member_casual STRING,                  -- String for membership type (casual or member)
            start_year INT,                        -- Integer for start year
            start_month INT,                       -- Integer for start month
            start_day INT,                         -- Integer for start day
            duration FLOAT                         -- Duration as float (time in seconds or minutes)
        );"""
        cursor.execute(create_table_sql)  # Executing table creation
        print("Target Table Initialized and Ready to Store Historical Citibike Data from ETL") # Acknowledging
        
        # Step 4: Use COPY INTO to load the CSV data into the table from the stage
        copy_sql = f"""
            COPY INTO {table}
            FROM @dev.raw_data.my_csv_rides_stage/cleaned_data.csv.gz
            FILE_FORMAT = (FORMAT_NAME = my_csv_format);
        """
        cursor.execute(copy_sql)  # Execute the COPY INTO command
        print("Data loaded successfully into Snowflake table.") # Acknowledging
        
        # Step 5: Commit the transaction if everything was successful
        cursor.execute("COMMIT;")
        print("Transaction committed. Data load complete.")
        
    except Exception as e:
        cursor.execute("ROLLBACK;")  # Roll back the transaction in case of any error to preserve the former contents
        print(f"An error occurred during historical citibike data load: {e}")
    
    finally: # Closing snowflake cursor and connection for efficient resource management
        cursor.close()
        conn.close()
    
    
    
# Airflow DAG definition
with DAG(
    dag_id="Citibike_Hist_Data_ETL",
    default_args={
        "owner": "Shatayu",
        "email": ["shatayu.thakur@sjsu.edu"],
        "email_on_failure": True,
        "email_on_retry": True,
        "email_on_success": True,
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    start_date=datetime(2024, 11, 20),
    catchup=False,
    tags=["ETL","Historical"],
    schedule_interval='30 21 * * *',  # This will run daily at 2:30 PM PT
) as dag:
    
    start_year = 2021
    start_month = 10
    end_year = 2024
    end_month = 10
    extracted_file_path = os.path.join(LOCAL_STORAGE_DIR, 'extracted_data.csv')
    cleaned_file_path = os.path.join(LOCAL_STORAGE_DIR, 'cleaned_data.csv')
    raw_data_table = 'dev.raw_data.citibike_historical'
    
    extract_task = extract_citibike_data(start_year, start_month, end_year, end_month)
    transform_task = transform_citibike_data(extracted_file_path)
    load_task = load_citibike_data(raw_data_table, cleaned_file_path)

    trigger_etl_historical_weather_task = TriggerDagRunOperator(
        task_id='trigger_etl_historical_weather',
        trigger_dag_id='Weather_Hist_Data_ETL',
        conf={},  # Pass any configuration needed by the triggered DAG
        wait_for_completion=True,  # Optionally wait for the triggered DAG to complete
    )

    
    # Defining sequence for ETL
    extract_task >> transform_task >> load_task >> trigger_etl_historical_weather_task