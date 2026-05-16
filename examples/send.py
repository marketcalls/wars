"""
Send a one-off text, image, or document. Requires a paired session
(run examples/pair.py first).

Usage:
    python examples/send.py text 919876543210 "Hello from wars"
    python examples/send.py image 919876543210 ./screenshot.png --caption "Dashboard"
    python examples/send.py doc   919876543210 ./report.pdf
"""

import argparse

from wars import WhatsApp


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["text", "image", "doc"])
    p.add_argument("to")
    p.add_argument("payload", help="Text body, or path to image/document")
    p.add_argument("--caption")
    p.add_argument("--db", default="whatsapp.db")
    args = p.parse_args()

    with WhatsApp(args.db) as wa:
        wa.connect()
        wa.wait_until_ready(timeout=60)

        if args.kind == "text":
            mid = wa.send_text(args.to, args.payload)
        elif args.kind == "image":
            mid = wa.send_image(args.to, args.payload, caption=args.caption)
        else:
            mid = wa.send_document(args.to, args.payload)

        print(f"sent: {mid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
