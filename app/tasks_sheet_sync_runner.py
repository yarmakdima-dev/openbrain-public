import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.job_runs import track_job
from app.tasks_sheet_sync import sync_tasks_tab
from app.ideas_sheet_sync import sync_ideas_tab
from app.people_sheet_sync import sync_people_tab
from app.projects_sheet_sync import sync_projects_tab
from app.books_sheet_sync import sync_books_tab
from app.highlights_sheet_sync import sync_highlights_tab

if __name__ == "__main__":
    exit_code = 0

    try:
        with track_job("tasks_sheet_sync"):
            result = sync_tasks_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"Tasks sync failed: {exc}")
        exit_code = 1

    try:
        with track_job("ideas_sheet_sync"):
            result = sync_ideas_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"Ideas sync failed: {exc}")
        exit_code = 1

    try:
        with track_job("people_sheet_sync"):
            result = sync_people_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"People sync failed: {exc}")
        exit_code = 1

    try:
        with track_job("projects_sheet_sync"):
            result = sync_projects_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"Projects sync failed: {exc}")
        exit_code = 1

    try:
        with track_job("books_sheet_sync"):
            result = sync_books_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"Books sync failed: {exc}")
        exit_code = 1

    try:
        with track_job("highlights_sheet_sync"):
            result = sync_highlights_tab()
            print(result.as_message())
            if result.errors:
                for e in result.errors:
                    print(f"  Warning: {e}")
    except Exception as exc:
        print(f"Highlights sync failed: {exc}")
        exit_code = 1

    raise SystemExit(exit_code)
