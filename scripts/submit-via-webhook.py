#!/usr/bin/env python3
"""
Webhook submission script for Life-USTC/static repository.

This script reads cached JSON data and submits it via webhook API.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def read_json_file(file_path: Path) -> Optional[Any]:
    """
    Read and parse a JSON file.
    
    Args:
        file_path: Path to the JSON file
        
    Returns:
        Parsed JSON data or None if file doesn't exist or is invalid
    """
    logger = logging.getLogger(__name__)
    
    if not file_path.exists():
        logger.warning(f"File not found: {file_path}")
        return None
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.debug(f"Successfully read {file_path}")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to read {file_path}: {e}")
        return None


def read_semesters(cache_root: Path) -> Optional[list]:
    """
    Read semesters data from cache.
    
    Args:
        cache_root: Root directory for cached data
        
    Returns:
        List of semesters or None if not found
    """
    logger = logging.getLogger(__name__)
    semester_path = cache_root / "catalog" / "api" / "teach" / "semester" / "list.json"
    
    logger.info(f"Reading semesters from: {semester_path}")
    semesters = read_json_file(semester_path)
    
    if semesters:
        logger.info(f"Found {len(semesters)} semesters")
    
    return semesters


def read_sections(cache_root: Path, semester_id: str) -> Optional[list]:
    """
    Read sections data for a specific semester from cache.
    
    Args:
        cache_root: Root directory for cached data
        semester_id: Semester ID to read sections for
        
    Returns:
        List of sections or None if not found
    """
    logger = logging.getLogger(__name__)
    sections_path = (
        cache_root / "catalog" / "api" / "teach" / "lesson" / 
        "list-for-teach" / f"{semester_id}.json"
    )
    
    logger.info(f"Reading sections for semester {semester_id} from: {sections_path}")
    sections = read_json_file(sections_path)
    
    if sections:
        logger.info(f"Found {len(sections)} sections for semester {semester_id}")
    
    return sections


def submit_webhook(
    webhook_url: str,
    data: dict,
    dry_run: bool = False
) -> bool:
    """
    Submit data to webhook endpoint.
    
    Args:
        webhook_url: URL of the webhook endpoint
        data: Data to submit
        dry_run: If True, only log what would be sent without actually sending
        
    Returns:
        True if submission was successful, False otherwise
    """
    logger = logging.getLogger(__name__)
    
    if dry_run:
        logger.info("DRY RUN: Would submit to webhook:")
        logger.info(f"  URL: {webhook_url}")
        logger.info(f"  Data keys: {list(data.keys())}")
        logger.info(f"  Data size: {len(json.dumps(data))} bytes")
        return True
    
    try:
        import requests
        
        logger.info(f"Submitting to webhook: {webhook_url}")
        response = requests.post(
            webhook_url,
            json=data,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        
        if response.status_code in (200, 201, 204):
            logger.info(f"Successfully submitted to webhook (status: {response.status_code})")
            return True
        else:
            logger.error(
                f"Webhook submission failed with status {response.status_code}: "
                f"{response.text}"
            )
            return False
            
    except ImportError:
        logger.error("requests library not installed. Install it with: pip install requests")
        return False
    except Exception as e:
        logger.error(f"Failed to submit to webhook: {e}")
        return False


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Submit cached data to webhook API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Submit data from default cache location (build/cache)
  python submit-via-webhook.py --webhook-url https://api.example.com/webhook

  # Submit data from custom cache location
  python submit-via-webhook.py --cache-root ./my-cache --webhook-url https://api.example.com/webhook

  # Dry run to see what would be submitted
  python submit-via-webhook.py --webhook-url https://api.example.com/webhook --dry-run

  # Submit only specific semesters
  python submit-via-webhook.py --webhook-url https://api.example.com/webhook --semester-ids 401 402
        """,
    )
    
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path("./build/cache"),
        help="Root directory for cached data (default: ./build/cache)",
    )
    
    parser.add_argument(
        "--webhook-url",
        type=str,
        help="URL of the webhook endpoint to submit data to",
    )
    
    parser.add_argument(
        "--semester-ids",
        type=str,
        nargs="+",
        help="Specific semester IDs to submit (default: all available)",
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be submitted without actually sending",
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)
    
    # Validate arguments
    if not args.webhook_url and not args.dry_run:
        parser.error("--webhook-url is required (or use --dry-run for testing)")
    
    cache_root = args.cache_root.resolve()
    
    if not cache_root.exists():
        logger.error(f"Cache root directory does not exist: {cache_root}")
        return 1
    
    logger.info(f"Using cache root: {cache_root}")
    
    # Read semesters
    semesters = read_semesters(cache_root)
    if not semesters:
        logger.error("No semesters data found")
        return 1
    
    # Filter semesters if specific IDs were requested
    if args.semester_ids:
        semester_id_set = set(args.semester_ids)
        semesters = [s for s in semesters if str(s.get("id")) in semester_id_set]
        logger.info(f"Filtered to {len(semesters)} semesters")
    
    if not semesters:
        logger.error("No semesters match the specified criteria")
        return 1
    
    # Process each semester
    success_count = 0
    fail_count = 0
    
    for semester in semesters:
        semester_id = str(semester.get("id"))
        semester_name = semester.get("nameZh", "Unknown")
        
        logger.info(f"Processing semester: {semester_name} (ID: {semester_id})")
        
        # Read sections for this semester
        sections = read_sections(cache_root, semester_id)
        
        if sections is None:
            logger.warning(f"No sections found for semester {semester_id}, skipping")
            continue
        
        # Prepare data for webhook
        webhook_data = {
            "semester": semester,
            "sections": sections,
        }
        
        # Submit to webhook
        if args.webhook_url:
            if submit_webhook(args.webhook_url, webhook_data, args.dry_run):
                success_count += 1
            else:
                fail_count += 1
        else:
            # Dry run without URL
            logger.info(f"Would submit semester {semester_id} with {len(sections)} sections")
            success_count += 1
    
    # Summary
    logger.info(f"Submission complete: {success_count} succeeded, {fail_count} failed")
    
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
