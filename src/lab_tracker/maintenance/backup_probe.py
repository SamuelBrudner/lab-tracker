"""Contained filesystem worker; only manifest metadata leaves this process."""

import json
import sys
from datetime import datetime

from lab_tracker.bounded_subprocess import ProcessDeadline
from lab_tracker.maintenance.config import Deployment
from lab_tracker.maintenance.probes import check_backup


def main() -> int:
    try:
        deployment = Deployment.model_validate_json(sys.argv[1])
        now = datetime.fromisoformat(sys.argv[2])
        result = check_backup(deployment, now, ProcessDeadline.after(float(sys.argv[3])))
        print(json.dumps(result))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
