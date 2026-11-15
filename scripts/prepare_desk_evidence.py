"""Refresh local measured matchup inputs without downloads or paid calls."""
import json
from spe.ingest.desk_evidence import refresh_local_evidence

if __name__ == "__main__":
    print(json.dumps(refresh_local_evidence(force=True), indent=2))
