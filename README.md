# Life@USTC Static

## Introduction

`master` branch holds the code to generate these static files. GitHub Actions publishes the generated output to GitHub Pages directly from the workflow artifact.

GitHub Actions are used to keep the GitHub Pages deployment up-to-date.

Each successful build also publishes a SQLite snapshot at
`https://static.life-ustc.tiankaima.dev/life-ustc-static.sqlite`. The snapshot
stores typed upstream curriculum responses in normalized SQLite tables.

The build also publishes:

- `https://static.life-ustc.tiankaima.dev/life-ustc-static-guesses.sqlite` for
  inferred relationships that are not directly keyed by upstream data.
- `https://static.life-ustc.tiankaima.dev/schemas/upstream/*.schema.json` for
  the JSON Schema contract of each upstream response stored in SQLite.

The previous generated curriculum JSON endpoints and upstream response cache
files are no longer built or published.

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
