WITH historical_ride_data AS (
    SELECT
        start_year AS year,
        start_month AS month,
        COUNT(*) AS total_rides,
        SUM(CASE WHEN member_casual = 'member' THEN 1 ELSE 0 END) AS member_rides,
        SUM(CASE WHEN member_casual = 'casual' THEN 1 ELSE 0 END) AS casual_rides,
        SUM(CASE WHEN rideable_type = 'electric_bike' THEN 1 ELSE 0 END) AS electric_bike_rides,
        SUM(CASE WHEN rideable_type = 'docked_bike' THEN 1 ELSE 0 END) AS docked_bike_rides,
        SUM(CASE WHEN rideable_type = 'classic_bike' THEN 1 ELSE 0 END) AS classic_bike_rides,
        AVG(duration) AS average_duration
    FROM {{ source('raw_data', 'citibike_historical') }}
    GROUP BY start_year, start_month
)

SELECT
    year,
    month,
    total_rides,
    member_rides,
    casual_rides,
    electric_bike_rides,
    docked_bike_rides,
    classic_bike_rides,
    ROUND(average_duration, 2) AS average_duration
FROM historical_ride_data
ORDER BY year, month
