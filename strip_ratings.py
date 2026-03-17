import json
import argparse

def main():
    p = argparse.ArgumentParser()
    p.add_argument("in_json")
    p.add_argument("out_json")
    args = p.parse_args()

    with open(args.in_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    for team in data.get("teams", []):
        team.pop("rating", None)

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"Wrote {args.out_json}")

if __name__ == "__main__":
    main()