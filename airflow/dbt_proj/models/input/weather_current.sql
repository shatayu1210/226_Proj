SELECT
  DATE,
  TEMP,
  TEMP_FEEL,
  HUMIDITY,
  RAIN,
  YEAR,
  MONTH,
  DAY,
  HOUR
FROM {{ source('raw_data', 'weather_current') }}
WHERE DATE IS NOT NULL --Avoiding missing data