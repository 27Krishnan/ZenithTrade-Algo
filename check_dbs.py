import sqlite3

def check_db(db_path, instrument):
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT date, long_state, long_exit_reason FROM daily_state WHERE instrument=? ORDER BY date DESC LIMIT 3", (instrument,))
        rows = cur.fetchall()
        print(f"--- {instrument} ---")
        for r in rows:
            print(r)
        conn.close()
    except Exception as e:
        print(f"Error reading {db_path}: {e}")

check_db('silver_strategy/silver_strategy.db', 'SILVER')
check_db('gold_strategy/gold_strategy.db', 'GOLD')
check_db('natural_gas_strategy/natural_gas_strategy.db', 'NATURALGAS')
