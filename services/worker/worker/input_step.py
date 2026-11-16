"""One input stage, owned by the locked/supervised production input lane."""
import argparse
import json

from spe.publishing.pipeline import input_operation, run_step


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=("production_inputs", "production_weather",
                                       "production_nflverse", "production_free_college"))
    args = parser.parse_args()
    print(json.dumps({"step": args.name, "result": run_step(args.name, input_operation(args.name))}, default=str), flush=True)
