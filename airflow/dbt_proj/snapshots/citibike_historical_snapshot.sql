{% snapshot citibike_historical_snapshot %}

{{
  config(
    target_schema='snapshots',   
    unique_key="RIDE_ID",          
    strategy='check',              
    check_cols=['RIDEABLE_TYPE', 'STARTED_AT', 'ENDED_AT', 'START_STATION_NAME', 'END_STATION_NAME'], 
    invalidate_hard_deletes=True
  )
}}

select
  RIDE_ID,
  RIDEABLE_TYPE,
  STARTED_AT,
  ENDED_AT,
  START_STATION_NAME,
  START_STATION_ID,
  END_STATION_NAME,
  END_STATION_ID,
  START_LAT,
  START_LNG,
  END_LAT,
  END_LNG,
  MEMBER_CASUAL,
  START_YEAR,
  START_MONTH,
  START_DAY,
  DURATION
from {{ source('raw_data', 'citibike_historical') }}

{% endsnapshot %}