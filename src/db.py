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
    conn.execute('DROP TABLE IF EXISTS select_strategies')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cached_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE NOT NULL,
            mcp_tool_sequence TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS select_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            signature TEXT,
            value TEXT NOT NULL,
            recipe TEXT NOT NULL,
            chosen_label TEXT,
            description TEXT,
            embedding TEXT,
            success_count INTEGER DEFAULT 1,
            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(domain, role, name)
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

def delete_flow(domain: str):
    """Removes a cached flow for a domain, forcing regeneration on next run."""
    conn = get_db_connection()
    conn.execute('DELETE FROM cached_flows WHERE domain = ?', (domain,))
    conn.commit()
    conn.close()

def delete_recipe(domain: str, role: str, name: str):
    """Removes a cached select recipe for one field, forcing fresh discovery on next resolve()."""
    conn = get_db_connection()
    conn.execute('DELETE FROM select_recipes WHERE domain = ? AND role = ? AND name = ?', (domain, role, name))
    conn.commit()
    conn.close()

def save_flow(domain: str, mcp_tool_sequence: list):
    """Saves or overwrites a flow sequence."""
    conn = get_db_connection()
    conn.execute(
        'INSERT OR REPLACE INTO cached_flows (domain, mcp_tool_sequence, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)',
        (domain, json.dumps(mcp_tool_sequence))
    )
    conn.commit()
    conn.close()

def get_recipe(domain: str, role: str, name: str) -> dict:
    """Returns the cached recipe row for this exact field, if any, as a dict
    with `recipe` and `embedding` already JSON-decoded."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM select_recipes WHERE domain = ? AND role = ? AND name = ?',
        (domain, role, name)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["recipe"] = json.loads(result["recipe"])
    result["embedding"] = json.loads(result["embedding"]) if result["embedding"] else None
    return result

def save_recipe(domain: str, role: str, name: str, signature: str, value: str,
                 recipe: list, chosen_label: str, description: str, embedding: list):
    """Records a successful recipe for this exact field. Repeated successes
    for the same field bump success_count and overwrite the rest."""
    conn = get_db_connection()
    conn.execute(
        '''INSERT INTO select_recipes (domain, role, name, signature, value, recipe, chosen_label, description, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain, role, name) DO UPDATE SET
             signature = excluded.signature,
             value = excluded.value,
             recipe = excluded.recipe,
             chosen_label = excluded.chosen_label,
             description = excluded.description,
             embedding = excluded.embedding,
             success_count = success_count + 1,
             last_used = CURRENT_TIMESTAMP''',
        (domain, role, name, signature, value, json.dumps(recipe), chosen_label, description,
         json.dumps(embedding) if embedding is not None else None)
    )
    conn.commit()
    conn.close()

def find_similar_recipes(embedding: list, domain: str = None, exclude_base_domain: str = None,
                          exclude=None, top_k: int = 3) -> list:
    """Returns the top-k recipes whose stored embedding is most similar
    (cosine similarity) to `embedding`, excluding the (domain, role, name)
    in `exclude` if given. Brute-force in Python -- fine at this scale.

    If `domain` is given, only rows for that domain are considered. This
    scopes hints to "this site's own history" -- including sibling fields
    discovered earlier in the same run, once their recipes are saved --
    rather than the whole global index, where a different domain's
    near-duplicate recipe for the same field/value would hand a fresh
    discovery its answer.

    If `exclude_base_domain` is given (and `domain` is not), rows whose
    domain -- with any "#..." test-namespace suffix stripped -- matches it
    are excluded. This is for cross-site hints: it excludes every namespace
    of the current real site (production AND test variants), so a hint can
    only come from a genuinely different real site."""
    from . import embeddings

    conn = get_db_connection()
    cursor = conn.cursor()
    if domain is not None:
        cursor.execute('SELECT * FROM select_recipes WHERE embedding IS NOT NULL AND domain = ?', (domain,))
    else:
        cursor.execute('SELECT * FROM select_recipes WHERE embedding IS NOT NULL')
    rows = cursor.fetchall()
    conn.close()

    scored = []
    for row in rows:
        if exclude and (row["domain"], row["role"], row["name"]) == tuple(exclude):
            continue
        if exclude_base_domain and row["domain"].split("#")[0] == exclude_base_domain:
            continue
        row_embedding = json.loads(row["embedding"])
        similarity = embeddings.cosine_similarity(embedding, row_embedding)
        scored.append((similarity, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    results = []
    for similarity, row in scored[:top_k]:
        result = dict(row)
        result["recipe"] = json.loads(result["recipe"])
        result["embedding"] = None
        result["similarity"] = similarity
        results.append(result)
    return results