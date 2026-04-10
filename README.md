# Life@USTC Static

## Introduction

`master` branch holds the code to generate these static files. GitHub Actions publishes the generated output to GitHub Pages directly from the workflow artifact.

GitHub Actions are used to keep the GitHub Pages deployment up-to-date.

Each successful build also publishes a SQLite snapshot at
`https://static.life-ustc.tiankaima.dev/life-ustc-static.sqlite`. The snapshot
contains structured parsed curriculum and bus data for downstream import
tooling.

## License & Warranty

WE PROVIDE ABSOLUTELY NO WARRANTY. USE THIS SOFTWARE AT YOUR OWN RISK.
