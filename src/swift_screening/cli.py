"""Command line: list management, ad-hoc screening, audit verification, demo, server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from swift_screening.config.models import hash_api_key, load_config
from swift_screening.lists.connectors.base import ListFetchError, ListFormatError
from swift_screening.lists.model import ListType
from swift_screening.lists.registry import FORMATS, connector_for, refresh_list
from swift_screening.lists.store import ListShrinkError, ListStore
from swift_screening.settings import Settings

FIXTURES = [
    ("FIXTURE_SANCTIONS", "json", "fixtures/lists/fixture_sanctions.json", ListType.SANCTIONS),
    ("FIXTURE_PEP", "json", "fixtures/lists/fixture_pep.json", ListType.PEP),
    ("FIXTURE_INTERNAL", "csv", "fixtures/lists/fixture_internal.csv", ListType.INTERNAL),
]


def _service(settings: Settings):  # type: ignore[no-untyped-def]
    from swift_screening.service import ScreeningService
    from swift_screening.storage.db import Database

    if settings.database_url.startswith("sqlite:///"):
        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(
            parents=True, exist_ok=True
        )
    db = Database(settings.database_url)
    db.create_all()
    return ScreeningService(ListStore(settings.list_store), load_config(settings.config), db), db


def cmd_lists_load(args: argparse.Namespace, settings: Settings) -> int:
    store = ListStore(settings.list_store)
    connector = connector_for(args.format, args.list_id, ListType(args.list_type))
    try:
        diff = refresh_list(store, connector, args.source, force=args.force)
    except ListShrinkError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 3
    except (ListFetchError, ListFormatError) as exc:
        print(f"FAILED (last good snapshot kept): {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "list_id": diff.list_id,
                "version": diff.to_version,
                "added": len(diff.added),
                "removed": len(diff.removed),
                "changed": len(diff.changed),
            }
        )
    )
    return 0


def cmd_lists_fixtures(_: argparse.Namespace, settings: Settings) -> int:
    store = ListStore(settings.list_store)
    for list_id, fmt, path, lt in FIXTURES:
        diff = refresh_list(store, connector_for(fmt, list_id, lt), path)
        print(f"{list_id}: {diff.to_version} (+{len(diff.added)} -{len(diff.removed)})")
    return 0


def cmd_lists_status(_: argparse.Namespace, settings: Settings) -> int:
    store = ListStore(settings.list_store)
    for lid in store.list_ids():
        print(store.status(lid, 36).model_dump_json())
    return 0


def cmd_screen(args: argparse.Namespace, settings: Settings) -> int:
    svc, _ = _service(settings)
    tenant = svc.config.tenant(args.tenant)
    if tenant is None:
        print(f"unknown tenant {args.tenant}", file=sys.stderr)
        return 2
    data = Path(args.file).read_bytes()
    result = (
        svc.screen_mx(data, tenant)
        if args.mx
        else svc.screen_mt(data.decode("utf-8", "replace"), tenant)
    )
    print(result.model_dump_json(indent=2))
    return {"PASS": 0, "REVIEW": 10, "BLOCK": 20}[result.decision.value]


def cmd_audit_verify(_: argparse.Namespace, settings: Settings) -> int:
    _, db = _service(settings)
    ok, bad, n = db.verify_chain()
    print(json.dumps({"ok": ok, "rows_checked": n, "first_bad_seq": bad}))
    return 0 if ok else 1


def cmd_purge(args: argparse.Namespace, settings: Settings) -> int:
    _, db = _service(settings)
    print(json.dumps({"redacted": db.purge_expired(args.days)}))
    return 0


def cmd_demo(_: argparse.Namespace, settings: Settings) -> int:
    from swift_screening.synth.generator import PartySpec, mt103, pacs008

    cmd_lists_fixtures(_, settings)
    svc, _db = _service(settings)
    tenant = svc.config.tenants[0]
    samples = {
        "clean MT103": mt103(seed=1),
        "MT103 beneficiary 'Muhammad Al Rashid'": mt103(
            beneficiary=PartySpec(name="Muhammad Al Rashid")
        ),
        "MT103 field 70 hides 'OBO Zarkhov Industrial'": mt103(
            remittance="INV 2231 OBO ZARKHOV INDUSTRIAL"
        ),
        "pacs.008 creditor in Sevastopol": pacs008(
            creditor=PartySpec(name="Harbour Supplies", town="SEVASTOPOL", country="UA")
        ),
    }
    for label, msg in samples.items():
        r = svc.screen_mx(msg, tenant) if msg.startswith("<?xml") else svc.screen_mt(msg, tenant)
        print(f"{r.decision.value:6}  {label}  reasons={r.reason_codes}")
        for h in r.hits:
            print(f"        - {h.explanation}")
        for g in r.geo_findings:
            print(f"        - {g.explanation}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from swift_screening.api.main import build_app

    uvicorn.run(build_app(settings), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swift-screening")
    sub = p.add_subparsers(dest="cmd", required=True)

    lists = sub.add_parser("lists").add_subparsers(dest="lists_cmd", required=True)
    ld = lists.add_parser("load", help="fetch/parse/store a list snapshot")
    ld.add_argument("--format", choices=sorted(FORMATS), required=True)
    ld.add_argument("--list-id")
    ld.add_argument("--list-type", default="sanctions", choices=[t.value for t in ListType])
    ld.add_argument("--source", help="URL or file path (defaults to the connector URL)")
    ld.add_argument("--force", action="store_true", help="accept a large shrink after review")
    ld.set_defaults(fn=cmd_lists_load)
    lists.add_parser("load-fixtures").set_defaults(fn=cmd_lists_fixtures)
    lists.add_parser("status").set_defaults(fn=cmd_lists_status)

    sc = sub.add_parser("screen", help="screen one message file")
    sc.add_argument("file")
    sc.add_argument("--mx", action="store_true")
    sc.add_argument("--tenant", default="demo")
    sc.set_defaults(fn=cmd_screen)

    audit = sub.add_parser("audit").add_subparsers(dest="audit_cmd", required=True)
    audit.add_parser("verify-chain").set_defaults(fn=cmd_audit_verify)

    pg = sub.add_parser("purge", help="redact stored results older than N days")
    pg.add_argument("--days", type=int, required=True)
    pg.set_defaults(fn=cmd_purge)

    hk = sub.add_parser("hash-key", help="print the SHA-256 to put in tenant config")
    hk.add_argument("key")
    hk.set_defaults(fn=lambda a, s: print(hash_api_key(a.key)) or 0)

    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return int(args.fn(args, Settings()))


if __name__ == "__main__":
    raise SystemExit(main())
