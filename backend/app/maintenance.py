"""python -m app.maintenance [migrate|cleanup-traces]"""

import sys

from app.core.config import Settings
from app.db.database import initialize
from app.db.models import db
from app.db.repository import cleanup

if __name__ == "__main__":
    initialize(Settings().database_path)
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup-traces":
        with db.connection_context():
            print(f"Removed {cleanup()} expired trace records")
