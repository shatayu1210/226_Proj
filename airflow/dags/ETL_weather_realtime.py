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
import json
import requests
import snowflake.connector
import pandas as pd


def return_snowflake_conn():
    hook = SnowflakeHook(snowflake_conn_id='snowflake_conn') # Initialize the SnowflakeHook
    conn = hook.get_conn()
    return conn, conn.cursor() # Returning connection (to close it once database operations are done) as well as cursor object (to operate on databases)


@task
def extract_weather_current_data():
    """
    Extract the realtime current data from Weather API for Jersey City, New Jersey.
    Args: extracted_data (data frame)
        
    Returns: pd.Dataframe Extracted Jersey City Realtime (Current) Data
    """
    weather_api_key = Variable.get('weather_api_key')
    City = 'Jersey City'

    weather_api_url = 'http://api.weatherapi.com/v1/current.json'
    final_url = weather_api_url + '?key=' + weather_api_key + '&q='  +City
    response = requests.get(final_url)
    output = response.json()
    print("Raw Output in JSON", output)

    # Extracting necessary values from the Weather API response
    last_updated = output['current']['last_updated']
    temp_f = output['current']['temp_f']
    feelslike_f = output['current']['feelslike_f']
    humidity = output['current']['humidity']
    precip_mm = output['current']['precip_mm']

    # Preparing data for the DataFrame
    current_data = {
        'last_updated': [last_updated],  # Timestamp as date
        'temp_f': [temp_f],
        'feelslike_f': [feelslike_f],
        'humidity': [humidity],
        'precip_mm': [precip_mm],
    }

    # Creating DataFrame
    extract_df = pd.DataFrame(current_data)

    # Printing the DataFrame
    print("Current Data for Jersey City, New Jersey: \n",extract_df)
    
    return extract_df

@task
def transform_weather_current_data(extracted_data):
    """
    Transform the data by excluding rows with null values.
    
    Args: extracted_data (data frame)
        
    Returns: pd.Dataframe Transformed Jersey City Realtime (Current) Data
    """
    
    extracted_df = extracted_data
    
    # Convert last_updated to a datetime column using pd.to_datetime
    extracted_df['last_updated_dt'] = pd.to_datetime(extracted_df['last_updated'], format='%Y-%m-%d %H:%M')
    
    # Extract year, month, day, hour from the datetime column    
    extracted_df['year'] = extracted_df['last_updated_dt'].dt.year
    extracted_df['month'] = extracted_df['last_updated_dt'].dt.month
    extracted_df['day'] = extracted_df['last_updated_dt'].dt.day
    extracted_df['hour'] = extracted_df['last_updated_dt'].dt.hour

    # Rename columns to match with historical weather data
    transformed_df = extracted_df.rename(columns={
    'last_updated_dt': 'date',
    'temp_f': 'temp',
    'feelslike_f': 'temp_feel',
    'precip_mm': 'rain'
    })[['date', 'temp', 'temp_feel', 'humidity', 'rain', 'year', 'month', 'day', 'hour']]

    # Printing the DataFrame
    print("Transformed Data for Jersey City, New Jersey: \n",transformed_df)
    
    # Printing Features
    print("Transformed Data Features: ", transformed_df.columns)
    
    return transformed_df

@task
def load_weather_current_data(table, transformed_data):
    """
    Load the transformed data to Snowflake with transaction.
    
    Args: table (STRING): target snowflake table
    transformed_data (pd.Dataframe): Transformed Realtime Weather from Transform Task Output
        
    Returns: Nothing
    """
    try:
        transformed_df = transformed_data
        
        conn, cursor = return_snowflake_conn()  # Initialize Snowflake connection and cursor
        cursor.execute("BEGIN;")  # Start the SQL transaction (Idempotency)

        print("Realtime Record to be pushed to snowflake table: ", transformed_df)

        cursor.execute("USE DATABASE DEV;")
        cursor.execute("USE SCHEMA RAW_DATA;")
        
        create_table_sql = f"""CREATE OR REPLACE TABLE {table} (
            date TIMESTAMP_NTZ(9),  -- Stores timestamp without timezone
            temp FLOAT,             -- Temperature
            temp_feel FLOAT,        -- Feels-like temperature
            humidity FLOAT,         -- Humidity percentage
            rain FLOAT,             -- Precipitation in mm
            year INT,               -- Year part of the timestamp
            month INT,              -- Month part of the timestamp
            day INT,                -- Day part of the timestamp
            hour INT                -- Hour part of the timestamp
        );"""
        cursor.execute(create_table_sql)  # Executing table creation
        print("Target Table Initialized and Ready to Store Realtime Weather Data from ETL") # Acknowledging
        
        # Insert each row of the dataframe into Snowflake
        for _, row in transformed_df.iterrows():
            # Convert timestamp to string format for Snowflake
            date_str = row['date'].strftime('%Y-%m-%d %H:%M:%S')
            
            # MERGE logic to prevent duplicate inserts based on 'date'
            merge_sql = f"""
            MERGE INTO {table} AS target
            USING (SELECT '{date_str}' AS date, {row['temp']} AS temp, {row['temp_feel']} AS temp_feel, 
                        {row['humidity']} AS humidity, {row['rain']} AS rain, {row['year']} AS year, 
                        {row['month']} AS month, {row['day']} AS day, {row['hour']} AS hour) AS source
            ON target.date = source.date
            WHEN MATCHED THEN 
                UPDATE SET target.temp = source.temp, target.temp_feel = source.temp_feel, 
                        target.humidity = source.humidity, target.rain = source.rain, 
                        target.year = source.year, target.month = source.month, 
                        target.day = source.day, target.hour = source.hour
            WHEN NOT MATCHED THEN
                INSERT (date, temp, temp_feel, humidity, rain, year, month, day, hour) 
                VALUES (source.date, source.temp, source.temp_feel, source.humidity, 
                        source.rain, source.year, source.month, source.day, source.hour);
            """
            
            # Execute the MERGE query to insert or update data
            cursor.execute(merge_sql)
            print(f"Inserted current weather for date: {row['date']}")
        print(f"Inserted current weather record successfully to {table} through query: {merge_sql}")
        
        # Commit the transaction if everything was successful
        cursor.execute("COMMIT;")
        print("Transaction committed. Data load complete.")
        
    except Exception as e:
        cursor.execute("ROLLBACK;")  # Roll back the transaction in case of any error to preserve the former contents
        print(f"An error occurred during realtime weather data load: {e}")
    
    finally:  # Closing snowflake cursor and connection for efficient resource management
        cursor.close()
        conn.close()

# Airflow DAG definition
with DAG(
    dag_id="Weather_Realtime_Data_ETL",
    default_args={
        "owner": "Shatayu",
        "email": ["shatayu.thakur@sjsu.edu"],
        "email_on_failure": True,
        "email_on_retry": True,
        "email_on_success": True,
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    description='Run realtime jersey city weather fetch every 30 minutes',
    schedule_interval='*/30 * * * *',
    start_date=datetime(2024, 11, 20),
    catchup=False,
    tags=["ETL","Realtime"],
) as dag:
    
    raw_data_table = "dev.raw_data.weather_current"
    # Extract Data
    extracted_df = extract_weather_current_data()

    # Transform Data (depends on extracted data)
    transformed_df = transform_weather_current_data(extracted_df)

    # Load Data (depends on transformed data)
    load_task = load_weather_current_data(raw_data_table, transformed_df)

    # Defining sequence for ETL
    extracted_df >> transformed_df >> load_task