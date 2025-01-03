SELECT
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
FROM {{ source('raw_data', 'citibike_historical') }}
WHERE DURATION <= 1500 --Excluding Outliers with over 1500mins of duration