"""Command line entry point.

    python -m solution ingest data/drop_1 [data/drop_2 ...] [--db au.db]
    python -m solution report --out reports/ [--db au.db]
    python -m solution reset [--db au.db]
"""
import argparse
from pathlib import Path

from . import db
from .ingest import ingest_drop
from .pii import Pii, load_master_key
from .report import write_reports
from .schema import load_dispositions, load_schemas

DEFAULT_DB = "au.db"


def cmd_ingest(args):
    conn = db.connect(args.db)
    pii = Pii(load_master_key())
    db.check_key_fingerprint(conn, pii.fingerprint)
    schemas = load_schemas()
    for drop_dir in args.drops:  # drops are processed in the order given
        ingest_drop(conn, pii, schemas, drop_dir)
    conn.close()


def cmd_report(args):
    conn = db.connect(args.db)
    write_reports(conn, load_dispositions(), args.out)
    conn.close()


def cmd_reset(args):
    """Delete the database file (plus SQLite's WAL side files). The PII key in .env is kept."""
    deleted = False
    for suffix in ["", "-wal", "-shm"]:
        path = Path(args.db + suffix)
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")
            deleted = True
    if not deleted:
        print(f"Nothing to delete: {args.db} does not exist")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m solution",
                                     description="Interaction ledger & reporting pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest one or more drop folders")
    p.add_argument("drops", nargs="+", help="drop folders, e.g. data/drop_1")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("report", help="write the five report files")
    p.add_argument("--out", required=True, help="output folder")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("reset", help="delete the database")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
