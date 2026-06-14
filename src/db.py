import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'flows.db')

def get_db_connection():
    # Ensure the data directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the SQLite schema."""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cached_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE NOT NULL,
            mcp_tool_sequence TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_cached_flow(domain: str) -> list:
    """Retrieves a cached flow as a Python list of dictionaries."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT mcp_tool_sequence FROM cached_flows WHERE domain = ?', (domain,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return json.loads(row['mcp_tool_sequence'])
    return None

def save_flow(domain: str, mcp_tool_sequence: list):
    """Saves or overwrites a flow sequence."""
    conn = get_db_connection()
    conn.execute(
        'INSERT OR REPLACE INTO cached_flows (domain, mcp_tool_sequence, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)',
        (domain, json.dumps(mcp_tool_sequence))
    )
    conn.commit()
    conn.close()