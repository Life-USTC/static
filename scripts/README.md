# Scripts

This directory contains utility scripts for the Life-USTC/static repository.

## submit-via-webhook.py

A Python helper script that reads cached JSON data and submits it via webhook API.

### Purpose

This script is designed to integrate with the webhook API described in Life-USTC/server-nextjs PR #1. It reads semester and section data from the local cache and submits it to a configured webhook endpoint.

### Requirements

- Python 3.13+
- `requests` library (for actual webhook submissions)

To install the requests library:
```bash
pip install requests
```

Or if using uv:
```bash
uv pip install requests
```

### Usage

#### Basic Usage

```bash
python scripts/submit-via-webhook.py --webhook-url https://api.example.com/webhook
```

This will:
1. Read semesters from `build/cache/catalog/api/teach/semester/list.json`
2. For each semester, read sections from `build/cache/catalog/api/teach/lesson/list-for-teach/{semester_id}.json`
3. Submit the data to the webhook endpoint

#### Custom Cache Location

```bash
python scripts/submit-via-webhook.py --cache-root ./custom-cache --webhook-url https://api.example.com/webhook
```

#### Dry Run (Testing)

Test what would be submitted without actually sending:

```bash
python scripts/submit-via-webhook.py --dry-run --verbose
```

#### Submit Specific Semesters

```bash
python scripts/submit-via-webhook.py --webhook-url https://api.example.com/webhook --semester-ids 401 402
```

#### Verbose Logging

```bash
python scripts/submit-via-webhook.py --webhook-url https://api.example.com/webhook --verbose
```

### Cache Structure

The script expects the following cache structure:

```
cache_root/
└── catalog/
    └── api/
        └── teach/
            ├── semester/
            │   └── list.json          # List of all semesters
            └── lesson/
                └── list-for-teach/
                    ├── 401.json       # Sections for semester 401
                    ├── 402.json       # Sections for semester 402
                    └── ...
```

### Data Format

#### Webhook Payload

For each semester, the script submits a JSON payload with the following structure:

```json
{
  "semester": {
    "id": 401,
    "nameZh": "2024秋季学期",
    "start": "2024-09-01",
    "end": "2025-01-15"
  },
  "sections": [
    {
      "id": 12345,
      "code": "MATH101-01",
      "course": {
        "cn": "高等数学A",
        "code": "MATH101"
      },
      "teacherAssignmentList": [...],
      "credits": 4.0,
      ...
    }
  ]
}
```

### Exit Codes

- `0`: Success
- `1`: Error (cache not found, webhook submission failed, etc.)
- `2`: Invalid arguments

### Integration with Build Process

To integrate with the GitHub Actions workflow, you can add a step after the build process:

```yaml
- name: Submit to Webhook
  env:
    WEBHOOK_URL: ${{ secrets.WEBHOOK_URL }}
  run: |
    uv pip install requests
    uv run python scripts/submit-via-webhook.py --webhook-url $WEBHOOK_URL
```

### Error Handling

The script includes comprehensive error handling:
- Missing cache directories
- Invalid JSON files
- Network errors during webhook submission
- Missing semester data

Errors are logged with appropriate log levels (ERROR, WARNING, INFO, DEBUG).
