"""Configuration for network reduction pipeline."""

DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 59734,
    'database': 'egon-data',
    'user': 'egon',
    'password': 'data',
}

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'

ORIGINAL_SCENARIO = 'eGon2025'
SOURCE_SCENARIO = 'eGon2025v3'
TARGET_SCENARIO = 'eGon2025v4'
