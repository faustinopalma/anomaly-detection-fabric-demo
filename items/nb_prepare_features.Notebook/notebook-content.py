# Fabric notebook source
#
# nb-prepare-features
# -------------------
# Reads raw telemetry from the lakehouse bronze layer, cleans + resamples
# per (machine, sensor), and writes feature tensors to the gold layer
# ready for training.
#
# Inputs  : Lakehouse `lh-telemetry` / Tables / bronze_telemetry
# Outputs : Lakehouse `lh-telemetry` / Tables / silver_telemetry
#                                              gold_windows_uni
#                                              gold_windows_multi

# CELL ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

WINDOW_SIZE   = 64       # timesteps per window
RESAMPLE_BIN  = "1s"     # alignment bin for multivariate windows
LOOKBACK_DAYS = 30

# CELL ********************

from pyspark.sql import functions as F, Window

bronze = spark.read.table("lh_telemetry.bronze_telemetry")

silver = (
    bronze
    .filter(F.col("quality") >= 192)
    .withColumn("ts", F.to_timestamp("ts"))
    .dropDuplicates(["machine_id", "sensor_id", "ts"])
)
silver.write.mode("overwrite").saveAsTable("lh_telemetry.silver_telemetry")

# CELL ********************

# Univariate windows: list of WINDOW_SIZE values per (machine, sensor, win_id)
w = Window.partitionBy("machine_id", "sensor_id").orderBy("ts")
uni = (
    silver
    .withColumn("rn", F.row_number().over(w) - 1)
    .withColumn("win_id", (F.col("rn") / F.lit(WINDOW_SIZE)).cast("long"))
    .groupBy("machine_id", "sensor_id", "win_id")
    .agg(
        F.min("ts").alias("window_start"),
        F.max("ts").alias("window_end"),
        F.collect_list("value").alias("values"),
    )
    .filter(F.size("values") == WINDOW_SIZE)
    .drop("win_id")
)
uni.write.mode("overwrite").saveAsTable("lh_telemetry.gold_windows_uni")

# CELL ********************

# Multivariate windows: pivot sensors into columns, then build windows per machine.
pivoted = (
    silver
    .withColumn("bin", F.window("ts", "1 second").start)
    .groupBy("machine_id", "bin")
    .pivot("sensor_id")
    .agg(F.avg("value"))
)

w2 = Window.partitionBy("machine_id").orderBy("bin")
multi = (
    pivoted
    .withColumn("rn", F.row_number().over(w2) - 1)
    .withColumn("win_id", (F.col("rn") / F.lit(WINDOW_SIZE)).cast("long"))
    .groupBy("machine_id", "win_id")
    .agg(F.collect_list(F.struct(*[c for c in pivoted.columns if c not in {"machine_id", "bin"}])).alias("rows"),
         F.min("bin").alias("window_start"),
         F.max("bin").alias("window_end"))
    .filter(F.size("rows") == WINDOW_SIZE)
    .drop("win_id")
)
multi.write.mode("overwrite").saveAsTable("lh_telemetry.gold_windows_multi")
