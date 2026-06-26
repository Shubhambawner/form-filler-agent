from .agent import client

EMBED_MODEL = "gemini-embedding-001"


def embed_text(text: str) -> list:
    """Returns the embedding vector for `text` as a list of floats."""
    if not text or not text.strip():
        raise ValueError("embed_text called with empty text")
    response = client.models.embed_content(model=EMBED_MODEL, contents=text)
    return response.embeddings[0].values


def cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
