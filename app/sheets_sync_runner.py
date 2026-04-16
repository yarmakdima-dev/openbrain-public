import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.job_runs import track_job
from app.sheets_sync import sync_google_sheet_bidirectional

if __name__ == "__main__":
    try:
        with track_job("sheets_sync"):
            result = sync_google_sheet_bidirectional()
            parts = []
            if result.pulled_count:
                parts.append(f"Pulled {result.pulled_count} updated entries from Google Sheets.")
            if result.updated_count:
                parts.append(f"Synced {result.synced_count} new entries to Google Sheets and updated {result.updated_count} existing rows.")
            else:
                parts.append(f"Synced {result.synced_count} new entries to Google Sheets.")
            print(" ".join(parts))
    except Exception as exc:
        print(f"Sync failed: {exc}")
        raise SystemExit(1)
