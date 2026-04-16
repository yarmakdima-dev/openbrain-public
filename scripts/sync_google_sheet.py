from app.sheets_sync import sync_entries_to_google_sheet

if __name__ == "__main__":
    result = sync_entries_to_google_sheet()
    print(f"Synced {result.synced_count} new entries to Google Sheets.")
