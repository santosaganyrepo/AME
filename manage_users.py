"""
Create and manage ExamManager teacher accounts (standalone — does not import
app.py and does not start the marking queue).

    python manage_users.py add <username> [--name "Display Name"]
    python manage_users.py passwd <username>
    python manage_users.py remove <username>
    python manage_users.py list

Passwords are typed at a hidden prompt (never on the command line, so they
don't end up in shell history). Changes take effect immediately — the app
does not need restarting.
"""

import argparse
import getpass
import sys

import users_store


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Password (min 8 characters): ")
        if len(pw) < users_store.MIN_PASSWORD_LEN:
            print(f"  Too short — use at least {users_store.MIN_PASSWORD_LEN} characters.")
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("  Passwords don't match — try again.")
            continue
        return pw


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Manage ExamManager teacher accounts (users.json).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create an account (or reset its password)")
    a.add_argument("username")
    a.add_argument("--name", help="display name, e.g. \"Mrs Nakato\"")
    pw = sub.add_parser("passwd", help="change an account's password")
    pw.add_argument("username")
    rm = sub.add_parser("remove", help="delete an account")
    rm.add_argument("username")
    sub.add_parser("list", help="list accounts")
    args = p.parse_args(argv)

    try:
        if args.cmd == "list":
            users = users_store.load_users()
            if not users:
                print("No accounts yet. Create one with: python manage_users.py add <username>")
            for name, rec in sorted(users.items()):
                print(f"  {name:20s} {rec.get('display_name', ''):25s} created {rec.get('created_at', '?')}")
            return 0

        if args.cmd == "add":
            existed = args.username.strip().lower() in users_store.load_users()
            if existed:
                print(f"'{args.username}' already exists — setting a new password.")
            created = users_store.set_user(args.username, _ask_password(), display_name=args.name)
            print(f"✅ Account '{args.username.strip().lower()}' {'created' if created else 'updated'}.")
            return 0

        if args.cmd == "passwd":
            if args.username.strip().lower() not in users_store.load_users():
                print(f"No account named '{args.username}'.")
                return 1
            users_store.set_user(args.username, _ask_password())
            print("✅ Password changed.")
            return 0

        if args.cmd == "remove":
            if users_store.remove_user(args.username):
                print(f"✅ Account '{args.username}' removed.")
                return 0
            print(f"No account named '{args.username}'.")
            return 1
    except ValueError as e:
        print(f"❌ {e}")
        return 1
    except KeyboardInterrupt:
        print()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
