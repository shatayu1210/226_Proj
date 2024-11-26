# Importing necessary modules
import pandas as pd
import os
from airflow import DAG
from airflow.models import Variable
from airflow.models import DagRun
from airflow.decorators import task
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook
from airflow.providers.http.hooks.http import HttpHook
from airflow.operators.dagrun_operator import TriggerDagRunOperator
from airflow.operators.python import PythonOperator
from io import StringIO

from datetime import datetime, timedelta
import requests
import snowflake.connector

import openmeteo_requests
import requests_cache
import pandas as pd
from retry_requests import retry

# Set up a directory for storing/accessing intermediate files
LOCAL_STORAGE_DIR = '/opt/airflow/temp_store_proj/weather'

def return_snowflake_conn():
    hook = SnowflakeHook(snowflake_conn_id='snowflake_conn') # Initialize the SnowflakeHook
    conn = hook.get_conn()
    return conn, conn.cursor() # Returning connection (to close it once database operations are done) as well as cursor object (to operate on databases)


@task
def extract_weather_hist_data():
    """
    Extract Jersey City Historical Weather data from Open-Meteo Archive for 2021-2024
    and return the extract as dataframe.
    
    Returns: pd.DataFrame: DataFrame containing Historical Jersey City Weather data.
    """
    
    # Setup the Open-Meteo API client with cache and retry on error
    cache_session = requests_cache.CachedSession('.cache', expire_after = -1)
    retry_session = retry(cache_session, retries = 5, backoff_factor = 0.2)
    openmeteo = openmeteo_requests.Client(session = retry_session)

    # Make sure all required weather variables are listed here
    # The order of variables in hourly or daily is important to assign them correctly below
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": 40.7178, #Coordinates for Jersey City (central approx)
        "longitude": 74.0431, #Coordinates for Jersey City (central approx)
        "start_date": "2021-10-01",
        "end_date": "2024-10-31",
        "hourly": ["temperature_2m", "relative_humidity_2m", "apparent_temperature", "rain"],
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph"
    }
    responses = openmeteo.weather_api(url, params=params)

    # Process first location. Add a for-loop for multiple locations or weather models
    response = responses[0]
    print(f"Coordinates {response.Latitude()}°N {response.Longitude()}°E")
    print(f"Elevation {response.Elevation()} m asl")
    print(f"Timezone {response.Timezone()} {response.TimezoneAbbreviation()}")
    print(f"Timezone difference to GMT+0 {response.UtcOffsetSeconds()} s")

    # Process hourly data. The order of variables needs to be the same as requested.
    hourly = response.Hourly()
    hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
    hourly_relative_humidity_2m = hourly.Variables(1).ValuesAsNumpy()
    hourly_apparent_temperature = hourly.Variables(2).ValuesAsNumpy()
    hourly_rain = hourly.Variables(3).ValuesAsNumpy()

    hourly_data = {"date": pd.date_range(
        start = pd.to_datetime(hourly.Time(), unit = "s", utc = True),
        end = pd.to_datetime(hourly.TimeEnd(), unit = "s", utc = True),
        freq = pd.Timedelta(seconds = hourly.Interval()),
        inclusive = "left"
    )}
    hourly_data["temp"] = hourly_temperature_2m
    hourly_data["temp_feel"] = hourly_apparent_temperature
    hourly_data["humidity"] = hourly_relative_humidity_2m
    hourly_data["rain"] = hourly_rain

    hourly_df = pd.DataFrame(data = hourly_data)

    # Assuming you have the dataframe `df`
    # Make sure the 'date' column is in datetime format
    hourly_df['date'] = pd.to_datetime(hourly_df['date'])

    # Extract year, month, day, and hour from the 'date' column
    hourly_df['year'] = hourly_df['date'].dt.year
    hourly_df['month'] = hourly_df['date'].dt.month
    hourly_df['day'] = hourly_df['date'].dt.day
    hourly_df['hour'] = hourly_df['date'].dt.hour

    # Extract the date part (without time) to group by day
    hourly_df['date_only'] = hourly_df['date'].dt.date

    # Calculate the day-wise max and min temperatures
    day_temp_max = hourly_df.groupby('date_only')['temp'].transform('max')
    day_temp_min = hourly_df.groupby('date_only')['temp'].transform('min')

    # Add the new columns to the dataframe
    hourly_df['day_temp_max'] = day_temp_max
    hourly_df['day_temp_min'] = day_temp_min

    # Optionally, drop the 'date_only' column if you don't need it
    hourly_df = hourly_df.drop(columns=['date_only'])

    print("Extracted Historical Weather Data Row Count: ", len(hourly_df))
    
    # Return the resulting dataframe
    return hourly_df


@task
def transform_weather_hist_data(extracted_data):
    """
    Transform the data by excluding rows with null values.
    
    Args: extracted_data (data frame)
        
    Returns: pd.Dataframe Transformed Jersey City Historical Data
    """
    
    # Ensure there are no null attributes in any record
    transformed_df = extracted_data.dropna()
    
    # Making sure the 'date' column is in datetime format
    transformed_df['date'] = pd.to_datetime(transformed_df['date'])

    # Extract year, month, day, and hour from the 'date' column
    transformed_df['year'] = transformed_df['date'].dt.year
    transformed_df['month'] = transformed_df['date'].dt.month
    transformed_df['day'] = transformed_df['date'].dt.day
    transformed_df['hour'] = transformed_df['date'].dt.hour

    # Extract the date part (without time) to group by day
    transformed_df['date_only'] = transformed_df['date'].dt.date

    # Calculate the day-wise max and min temperatures
    day_temp_max = transformed_df.groupby('date_only')['temp'].transform('max')
    day_temp_min = transformed_df.groupby('date_only')['temp'].transform('min')

    # Add the new columns to the dataframe
    transformed_df['day_temp_max'] = day_temp_max
    transformed_df['day_temp_min'] = day_temp_min

    # Optionally, drop the 'date_only' column if you don't need it
    transformed_df = transformed_df.drop(columns=['date_only'])

    # Printing features and length for tracking purpose
    print("Historical Weather Dataset Features Post Transformation: ", transformed_df.columns)
    print("Transformed Weather Dataset Row Count: ", len(transformed_df))
    
    # Return the resulting dataframe
    return transformed_df
    

@task
def load_weather_hist_data(table, transformed_data):
    """
    Load the transformed data to Snowflake with transaction.
    
    Args: table (STRING): target snowflake table
    transformed_df (pd.Dataframe): Transformed Output from Transform Task 
        
    Returns: Nothing
    """
    try:
        transformed_df = transformed_data  # This extracts the actual DataFrame from XCom

        conn, cursor = return_snowflake_conn()  # Initialize Snowflake connection and cursor
        cursor.execute("BEGIN;")  # Start the SQL transaction (Idempotency)

        print("Row count for data to be pushed: ", len(transformed_df))
        
        cursor.execute("USE DATABASE DEV;")
        cursor.execute("USE SCHEMA RAW_DATA;")

        # Step 1: Create file format for CSV
        create_file_format_sql = """CREATE OR REPLACE FILE FORMAT my_csv_weather_format
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

        # Step 2: Create stage (if not already exists)
        create_stage_sql = """
            CREATE OR REPLACE STAGE dev.raw_data.my_csv_weather_stage
                FILE_FORMAT = (FORMAT_NAME = my_csv_weather_format);
        """
        cursor.execute(create_stage_sql)  # Execute stage creation for loading CSV data
        print("Stage created successfully.") # Acknowledging
        
        # Step 3: Convert DataFrame to CSV format in-memory using StringIO
        output_file = os.path.join(LOCAL_STORAGE_DIR, 'cleaned_weather_data.csv') # Declaring output path for this function (store the transformed data)
        transformed_df.to_csv(output_file, index=False)

        # Step 4: Upload the CSV data from memory to the Snowflake stage
        put_sql = f"""
        PUT file://{output_file} @dev.raw_data.my_csv_weather_stage;
        """
        cursor.execute(put_sql)
        print("Data uploaded to stage successfully.") # Acknowledging
        
        # Step 5: Create or replace table
        create_table_sql = f"""
            CREATE OR REPLACE TABLE {table} (
            date TIMESTAMP_NTZ(9),          -- Timestamp with no time zone and 9-digit precision
            temp FLOAT,                     -- Temperature value as a float
            temp_feel FLOAT,                -- Feels-like temperature value as a float
            humidity FLOAT,                 -- Humidity percentage as a float
            rain FLOAT,                     -- Rain measurement as a float (could be 0 or a value)
            year INT,                       -- Year as an integer
            month INT,                      -- Month as an integer
            day INT,                        -- Day of the month as an integer
            hour INT,                       -- Hour of the day as an integer (0-23)
            day_temp_max FLOAT,             -- Maximum temperature for the day
            day_temp_min FLOAT              -- Minimum temperature for the day
        );
        """
        cursor.execute(create_table_sql)  # Executing table creation
        print("Target Table Initialized and Ready to Store Historical Weather Data from ETL") # Acknowledging
        
        # Step 6: Use COPY INTO to load the CSV data into the table from the stage
        copy_sql = f"""
            COPY INTO {table}
            FROM @dev.raw_data.my_csv_weather_stage
            FILE_FORMAT = (FORMAT_NAME = my_csv_weather_format);
        """
        cursor.execute(copy_sql)  # Execute the COPY INTO command
        print("Data loaded successfully into Snowflake table.") # Acknowledging
        
        # Step 7: Commit the transaction if everything was successful
        cursor.execute("COMMIT;")
        print("Transaction committed. Data load complete.")
        
    except Exception as e:
        cursor.execute("ROLLBACK;")  # Roll back the transaction in case of any error to preserve the former contents
        print(f"An error occurred during historical weather data load: {e}")
    
    finally:  # Closing snowflake cursor and connection for efficient resource management
        cursor.close()
        conn.close()


# Airflow DAG definition
with DAG(
    dag_id="Weather_Hist_Data_ETL",
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
    schedule_interval=None,  # This will be called by Citibike_Hist_Data_ETL
) as dag:
    
    raw_data_table = "dev.raw_data.weather_historical"
    
    # Extract Data
    extracted_df = extract_weather_hist_data()

    # Transform Data (depends on extracted data)
    transformed_df = transform_weather_hist_data(extracted_df)

    # Load Data (depends on transformed data)
    load_task = load_weather_hist_data(raw_data_table, transformed_df)

    trigger_etl_realtime_weather_task = TriggerDagRunOperator(
        task_id='trigger_etl_realtime_weather',
        trigger_dag_id='Weather_Realtime_Data_ETL',
        conf={},  # Pass any configuration needed by the triggered DAG
        wait_for_completion=True,  # Optionally wait for the triggered DAG to complete
    )
    
    # Defining sequence for ETL
    extracted_df >> transformed_df >> load_task >> trigger_etl_realtime_weather_task