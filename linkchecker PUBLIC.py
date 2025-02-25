import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import time
import asyncio
import json
from datetime import datetime, timedelta
import pytz  # Add this import for timezone handling
import os
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

# Set up Google Sheets credentials
scope = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']

# Modify the credentials setup
if os.getenv('GOOGLE_CREDENTIALS'):
    # Use credentials from environment variable
    import json
    credentials_dict = json.loads(os.getenv('GOOGLE_CREDENTIALS'))
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
else:
    # Use local file for development
    credentials = ServiceAccountCredentials.from_json_keyfile_name('sheetscredentials.json', scope)

creds = gspread.authorize(credentials)

# After loading credentials
print("Service Account Email:", credentials._service_account_email)
try:
    # Try to list all spreadsheets to verify credentials
    all_sheets = creds.openall()
    print(f"Successfully authenticated. Can access {len(all_sheets)} sheets.")
except Exception as e:
    print(f"Authentication error: {str(e)}")

# Set up Slack webhook - get from environment variable
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL')
SHEET_URL = os.getenv('SHEET_URL', '14Yk8UnQviC29ascf4frQfAEDWzM2_bp1UloRcnW8ZCg')
COLUMN_TO_CHECK = 'C'
CHECK_INTERVAL = 180  # 3 minutes in seconds

def send_slack_message(message):
    payload = {'text': message}
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Slack message: {e}")

def is_valid_url(url):
    # Basic URL validation
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(url_pattern.match(url))

async def check_links():
    try:
        print("Attempting to connect to Google Sheet...")
        
        try:
            spreadsheet = creds.open_by_key(SHEET_URL)
            
            # Get the specific worksheet by gid
            target_gid = 0  # Update this if your new sheet has a different gid
            sheet = None
            for ws in spreadsheet.worksheets():
                if ws.id == target_gid:
                    sheet = ws
                    break
                    
            if not sheet:
                raise Exception(f"Could not find worksheet with gid {target_gid}")
            
            print(f"\nAccessing worksheet: {sheet.title}")
            
            # Get all values
            all_values = sheet.get_all_values()
            total_rows = len(all_values) - 1  # Subtract header row
            print(f"\nTotal rows in sheet (excluding header): {total_rows}")
            
            # Get domain column (Column C, index 2)
            domains = []
            skipped_empty = 0
            
            for i, row in enumerate(all_values[1:], 1):  # Skip header
                if len(row) > 2:  # Make sure we have column C
                    domain = row[2].strip()  # Index 2 for Column C
                    if not domain:
                        skipped_empty += 1
                        continue
                        
                    # Add http:// if no protocol specified
                    if not (domain.startswith('http://') or domain.startswith('https://')):
                        domain = 'http://' + domain
                        
                    domains.append(domain)
                    print(f"Found URL in row {i}: {domain}")
            
            print(f"\nAnalysis:")
            print(f"Total rows processed: {total_rows}")
            print(f"Empty domains skipped: {skipped_empty}")
            print(f"Valid URLs found: {len(domains)}")
            
            print(f"\nFirst few URLs to check:")
            for d in domains[:5]:
                print(f"  {d}")
            
            failing_domains = []
            checked_count = 0
            
            print(f"\nStarting URL check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            for domain in domains:
                checked_count += 1
                print(f"Checking URL {checked_count}: {domain}")
                
                try:
                    response = requests.get(domain, timeout=30)
                    
                    # Skip 404 errors as they're considered "good" now
                    if response.status_code == 404:
                        print(f"✓ URL returns 404 (acceptable): {domain}")
                        continue
                        
                    # Check for domain expiration in the response text
                    response_text = response.text.lower()
                    
                    # Common expiration patterns
                    expiration_patterns = [
                        "the domain has expired",
                        "is this your domain?",
                        "renew now",
                        "this domain has expired",
                        "domain registration has expired",
                        "this domain name expired",
                        "this domain is expired",
                        "domain has been expired",
                        "domain renewal",
                        "expired domain",
                        "domain expiration",
                        "domain expired on"
                    ]
                    
                    # Check for specific text patterns
                    expired = False
                    for pattern in expiration_patterns:
                        if pattern in response_text:
                            expired = True
                            break
                    
                    # Additional check for the specific pattern from your screenshot
                    if ("domain has expired" in response_text and 
                        "is this your domain" in response_text and 
                        "renew now" in response_text):
                        expired = True
                    
                    if expired:
                        error_msg = f"🕒 Expired domain detected: {domain}\nReason: Domain expiration page detected"
                        failing_domains.append(error_msg)
                        print(error_msg)
                        continue
                        
                except requests.exceptions.RequestException as e:
                    # Only report connection errors and timeouts
                    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                        error_msg = f"⚠️ Cannot reach URL: {domain}\nError: Connection failed or timed out"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    else:
                        print(f"Skipping other error for {domain}: {str(e)}")
            
            print(f"\nChecked {checked_count} URLs")
            
            if failing_domains:
                batch_size = 20
                for i in range(0, len(failing_domains), batch_size):
                    batch = failing_domains[i:i + batch_size]
                    message = "URL Check Results:\n" + "\n".join(batch)
                    send_slack_message(message)
            else:
                print("All URLs are healthy")
                send_slack_message("✅ All URLs are functioning correctly")
                
        except gspread.exceptions.APIError as e:
            error_msg = f"API Error when accessing spreadsheet: {str(e)}"
            print(error_msg)
            send_slack_message(f"❌ {error_msg}")
        except Exception as e:
            error_msg = f"Error accessing worksheet: {str(e)}"
            print(error_msg)
            send_slack_message(f"❌ {error_msg}")
            
    except Exception as e:
        print(f"Error in check_links: {e}")
        send_slack_message(f"❌ Error in check_links: {str(e)}")

async def main():
    print("Starting link checker service...")
    
    # Initial startup delay to ensure deployment is complete
    startup_delay = 60  # 1 minute
    print(f"Waiting {startup_delay} seconds for deployment to stabilize...")
    await asyncio.sleep(startup_delay)
    
    print("Service started successfully!")
    send_slack_message("🚀 Link checker service started")
    
    while True:
        print("\nStarting URL check cycle...")
        await check_links()
        print(f"\nWaiting {CHECK_INTERVAL/60} minutes until next check")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        import pytz
    except ImportError:
        print("Installing required package: pytz")
        import subprocess
        subprocess.check_call(["pip", "install", "pytz"])
        import pytz
    
    # Add startup delay to ensure proper deployment
    print("Service initializing...")
    time.sleep(30)  # Wait 30 seconds for deployment to stabilize
    
    asyncio.run(main())