"""
Runs the living memory over messages that were already in ame.db before it
existed (the app consolidates new messages on its own). Run it from the
project folder with Ame stopped:

    python tools/seed_memory.py --from 1 --dry-run   (only counts)
    python tools/seed_memory.py --from 1

Each batch (up to ~24,000 characters of messages) is one call to the cheap
model of the active engine.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import database as db  # noqa: E402
import living_memory  # noqa: E402



def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_id", type=int, required=True,
                    help="first message id to process")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    living_memory._migrate_old_layout()
    rows = living_memory._messages_since(args.from_id - 1)
    calls, size = (1 if rows else 0), 0
    for r in rows:
        if size + len(r["content"]) > living_memory.INPUT_CAP and size:
            calls, size = calls + 1, 0
        size += len(r["content"])
    print(f"{len(rows)} messages from id {args.from_id} → about {calls} calls.")
    if args.dry_run or not rows:
        return
    living_memory._save_state({"cursor_id": args.from_id - 1, "ts": 0})
    while True:
        before = living_memory._read_state()["cursor_id"]
        log = living_memory.consolidate(force=True)
        print(f"batch up to id {living_memory._read_state()['cursor_id']}: {len(log)} changes")
        if living_memory._read_state()["cursor_id"] == before:
            break
    print("Done. Check the memories/ folder.")


if __name__ == "__main__":
    main()
