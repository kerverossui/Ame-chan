"""
Recomputes the semantic-search vectors of every message (and its chunks)
with the embedding model in memory.py. Needed only if that model changes.
The message text is never touched.

Run it from the project folder, with Ame stopped and the venv active:

    python tools/reembed_all.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import database as db  # noqa: E402
import memory  # noqa: E402


def main() -> None:
    if not os.path.exists(db.DB_PATH):
        sys.exit("No ame.db here yet — nothing to do.")
    with db.db_ctx() as conn:
        rows = conn.execute("SELECT id, content FROM messages ORDER BY id").fetchall()
    if not rows:
        print("No messages yet — nothing to do.")
        return
    answer = input(f"Recompute the vectors of {len(rows)} messages? Ame must be stopped. [y/N] ")
    if not answer.strip().lower().startswith("y"):
        print("Cancelled.")
        return
    start = time.time()
    for i, r in enumerate(rows, start=1):
        vec = memory.embed(r["content"]).astype("float32").tobytes()
        db.update_message_embedding(r["id"], vec)
        memory.index_chunks(r["id"], r["content"])
        if i % 100 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)}")
    print(f"Done in {time.time() - start:.0f}s.")


if __name__ == "__main__":
    main()
