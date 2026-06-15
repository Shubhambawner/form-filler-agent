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

    # Pre-variant-caching `cached_flows` had `UNIQUE(domain)` and no
    # snapshot/embedding columns -- a single row per domain that healing
    # would overwrite. There's no way to retrofit those old rows with an
    # initial snapshot, so drop and recreate; flows simply get rediscovered
    # (and cached as the first variant) on next use.
    cursor = conn.execute("PRAGMA table_info(cached_flows)")
    existing_cols = [row[1] for row in cursor.fetchall()]
    if existing_cols and "embedding" not in existing_cols:
        conn.execute('DROP TABLE cached_flows')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS cached_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            initial_snapshot TEXT NOT NULL,
            embedding TEXT NOT NULL,
            mcp_tool_sequence TEXT NOT NULL,
            success_count INTEGER DEFAULT 1,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cached_flows_domain ON cached_flows(domain)')
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

def find_best_flow(domain: str, embedding: list) -> dict:
    """Returns the cached flow variant for `domain` whose initial-page-snapshot
    embedding is most similar (cosine) to `embedding`, or None if no variants
    are stored for this domain yet.

    A "variant" is one row: a (initial_snapshot, mcp_tool_sequence) pair
    captured when that flow was discovered/healed. Different job
    listings/vendors under the same ATS domain can have different field sets
    and get their own variant rows instead of overwriting each other; this
    picks whichever stored variant's starting page looked most like the
    current one. Brute-force in Python -- fine at this scale."""
    from . import embeddings

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM cached_flows WHERE domain = ?', (domain,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None

    best_row, best_similarity = None, -1.0
    for row in rows:
        row_embedding = json.loads(row["embedding"])
        similarity = embeddings.cosine_similarity(embedding, row_embedding)
        if similarity > best_similarity:
            best_row, best_similarity = row, similarity

    result = dict(best_row)
    result["mcp_tool_sequence"] = json.loads(result["mcp_tool_sequence"])
    result["embedding"] = json.loads(result["embedding"])
    result["similarity"] = best_similarity
    return result

def get_flow_variants(domain: str) -> list:
    """Returns all cached flow variant rows for `domain` (id, success_count,
    last_updated, initial_snapshot, plus parsed mcp_tool_sequence/embedding),
    ordered by id. Mainly for test/debug inspection of how many variants
    exist and whether a save updated one in place vs. inserted a new one."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM cached_flows WHERE domain = ? ORDER BY id', (domain,))
    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        result = dict(row)
        result["mcp_tool_sequence"] = json.loads(result["mcp_tool_sequence"])
        result["embedding"] = json.loads(result["embedding"])
        results.append(result)
    return results

def delete_flow(domain: str):
    """Removes all cached flow variants for a domain, forcing regeneration on next run."""
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

def save_flow_variant(domain: str, initial_snapshot: str, embedding: list, mcp_tool_sequence: list,
                       update_id: int = None):
    """Stores `mcp_tool_sequence` as a flow variant for `domain`, keyed off
    the page's `initial_snapshot`/`embedding` captured before any actions.

    If `update_id` is given, overwrites that existing variant row in place
    (the "page changed slightly, re-heal the same listing" case). Otherwise
    inserts a new variant row (the "different listing/template with a
    different field set" case) -- this is what lets multiple variants coexist
    under one domain instead of repeatedly overwriting each other."""
    conn = get_db_connection()
    if update_id is not None:
        conn.execute(
            '''UPDATE cached_flows SET initial_snapshot = ?, embedding = ?, mcp_tool_sequence = ?,
               success_count = success_count + 1, last_updated = CURRENT_TIMESTAMP WHERE id = ?''',
            (initial_snapshot, json.dumps(embedding), json.dumps(mcp_tool_sequence), update_id)
        )
    else:
        conn.execute(
            '''INSERT INTO cached_flows (domain, initial_snapshot, embedding, mcp_tool_sequence)
               VALUES (?, ?, ?, ?)''',
            (domain, initial_snapshot, json.dumps(embedding), json.dumps(mcp_tool_sequence))
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