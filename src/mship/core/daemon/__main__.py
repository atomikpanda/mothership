"""`python -m mship.core.daemon` — fallback exec form when no `mshipd` script
is resolvable (the `python -m mship.ci.version_bump` precedent)."""
import sys

from mship.core.daemon.run import main

sys.exit(main())
