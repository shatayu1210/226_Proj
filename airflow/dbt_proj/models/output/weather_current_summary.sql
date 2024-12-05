WITH comfort_index_logic AS (
    SELECT
        date,
        temp,
        temp_feel,
        humidity,
        rain,
        year,
        month,
        day,
        hour,
        CASE 
            WHEN rain > 20 THEN 3
            WHEN rain > 10 AND rain <= 20 THEN 3
            WHEN rain <= 10 AND humidity > 80 AND temp > 85 THEN 1
            WHEN rain <= 10 AND humidity < 40 AND temp BETWEEN 60 AND 75 THEN 2
            ELSE 4
        END AS comfort_index
    FROM {{ source('raw_data', 'weather_current') }}
)

SELECT *
FROM comfort_index_logic
