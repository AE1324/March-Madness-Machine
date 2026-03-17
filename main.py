import argparse

from db import SessionLocal, init_db
from load_bracket import load_bracket_from_json
from simulate import generate_brackets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="March Madness bracket simulator")
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize the database schema and exit.",
    )
    parser.add_argument(
        "--load-bracket",
        metavar="JSON_PATH",
        help="Path to a bracket JSON file to load into the database.",
    )
    parser.add_argument(
        "--generate",
        type=int,
        metavar="N",
        help="Generate N brackets using the current model and bracket.",
    )
    parser.add_argument(
        "--model-version",
        type=str,
        default="v1",
        help="Optional label for the model version used.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.init_db:
        init_db()
        return

    session = SessionLocal()

    try:
        if args.load_bracket:
            load_bracket_from_json(session, args.load_bracket)

        if args.generate:
            generate_brackets(session, n=args.generate, model_version=args.model_version)
    finally:
        session.close()


if __name__ == "__main__":
    main()

