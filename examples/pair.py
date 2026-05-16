"""
One-time pairing helper.

Defaults: in-memory session (nothing written to disk), terminal QR only.

  python examples/pair.py                          # QR in terminal
  python examples/pair.py --phone 919876543210     # + 8-char pair code
  python examples/pair.py --db whatsapp.db         # persist to a file
  python examples/pair.py --save-png qr.png        # also save PNG (opt-in)
"""

import argparse
import sys

from wars import WhatsApp


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--db",
        default=None,
        help="SQLite path. Omit for in-memory (no files written).",
    )
    p.add_argument(
        "--phone",
        help="E.164 digits, e.g. 919876543210 (enables pair code alongside QR)",
    )
    p.add_argument(
        "--save-png",
        metavar="PATH",
        help="Opt-in: also write the QR as a PNG to PATH. Default is no file.",
    )
    args = p.parse_args()

    wa = WhatsApp(args.db, log_level="error")

    qr_count = {"n": 0}

    @wa.on_qr
    def show_qr(code: str) -> None:
        qr_count["n"] += 1
        # Clear screen so only the current QR is visible — old ones rotating
        # off the top makes it confusing to know which one to scan.
        print("\033[2J\033[H", end="", flush=True)
        print(f"WhatsApp pairing QR #{qr_count['n']} (refreshes ~every 30s)", flush=True)
        print("Phone > WhatsApp > Linked devices > Link a device\n", flush=True)
        WhatsApp.print_qr(code)
        if args.save_png:
            try:
                import qrcode  # type: ignore

                qrcode.make(code).save(args.save_png)
                print(f"\nAlso saved to {args.save_png}", flush=True)
            except Exception as e:
                print(f"\n(could not write {args.save_png}: {e})", flush=True)

    @wa.on_pair_code
    def show_code(code: str) -> None:
        print("\n=== Pair code (8 chars) ===", flush=True)
        print(f"    {code}", flush=True)
        print("Enter on phone: Linked devices > Link with phone number\n", flush=True)

    @wa.on_connected
    def connected() -> None:
        if args.db:
            print(f"✔ Paired. Session saved to {args.db}", flush=True)
        else:
            print(
                "✔ Paired (in-memory). Session will be lost when this script "
                "exits — for persistence, re-run with `--db whatsapp.db` or "
                "use `wa.export_session()` to stash bytes in your own DB.",
                flush=True,
            )

    wa.connect(phone=args.phone)
    try:
        wa.wait_until_ready(timeout=300)
    except TimeoutError:
        print("Timed out. Try again.", file=sys.stderr)
        return 1
    wa.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
