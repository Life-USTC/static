# Life@USTC Static

Builds upstream USTC curriculum/bus snapshots and publishes them to GitHub Pages
from `master` via GitHub Actions. The Life@USTC **server** static loader
consumes these SQLite artifacts (not a user-facing product surface).

Published artifacts:

- `https://static.life-ustc.tiankaima.dev/life-ustc-static.sqlite` — typed upstream responses in normalized tables
- `https://static.life-ustc.tiankaima.dev/life-ustc-static-guesses.sqlite` — inferred relationships
- `https://static.life-ustc.tiankaima.dev/schemas/upstream/*.schema.json` — JSON Schema per upstream response

Legacy curriculum JSON endpoints and upstream response cache files are no longer
built.

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
