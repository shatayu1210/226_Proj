{% snapshot weather_current_snapshot %}

{{
  config(
    target_schema='snapshots',   
    unique_key="DATE",          
    strategy='check',              
    check_cols=['TEMP', 'TEMP_FEEL', 'HUMIDITY', 'RAIN'], 
    invalidate_hard_deletes=True
  )
}}

select
  DATE,
  TEMP,
  TEMP_FEEL,
  HUMIDITY,
  RAIN,
  YEAR,
  MONTH,
  DAY,
  HOUR
from {{ source('raw_data', 'weather_current') }}

{% endsnapshot %}