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
from bs4 import BeautifulSoup  # Add BeautifulSoup for better HTML parsing

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

def get_domain_expiration_indicators():
    return [
        # Basic expiration messages
        'domain has expired', 'is this your domain', 'renew now',
        'domain registration expired', 'this domain has expired',
        'domain name has expired', 'domain expired', 'expired domain',
        'renew domain', 'domain not found', 'domain renewal',
        'this domain is not active', 'domain has been expired',
        'domain expiration notice',
        
        # Additional subtle indicators
        'this domain may be for sale',
        'buy this domain',
        'domain seized',
        'domain auction',
        'domain listed',
        'backorder this domain',
        'inquire about this domain',
        'purchase this domain',
        'this webpage is not available',
        'this site is temporarily unavailable',
        'website expired',
        'account suspended',
        'this domain is pending renewal or has expired',
        'domain registration is pending',
        
        # Parking service indicators
        'parked domain',
        'domain parking',
        'this domain is parked',
        'domain holder',
        'domain registered',
        
        # Registration-related
        'registration expired',
        'registrar holding page',
        'register this domain',
        'domain registration',
        'registration services',
        
        # Common parking services
        'sedoparking',
        'hugedomains',
        'godaddy auctions',
        'namesilo parking',
        'domain registration pending',
    ]

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
                    response = requests.get(domain, timeout=30, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    })
                    
                    # Skip 404 errors as they're considered "good" now
                    if response.status_code == 404:
                        print(f"✓ URL returns 404 (acceptable): {domain}")
                        continue
                    
                    # Get the full page content
                    response_text = response.text.lower()
                    
                    # Parse HTML for better analysis
                    soup = BeautifulSoup(response_text, 'html.parser')
                    
                    # Check meta tags and title
                    meta_content = ' '.join([meta.get('content', '').lower() for meta in soup.find_all('meta')])
                    title_content = soup.title.string.lower() if soup.title else ''
                    
                    # Get all text content including hidden elements
                    all_text = ' '.join([text.lower() for text in soup.stripped_strings])
                    
                    # Combine all content for checking
                    combined_content = f"{response_text} {meta_content} {title_content} {all_text}"
                    
                    # Get expiration indicators
                    expiration_indicators = get_domain_expiration_indicators()
                    
                    # Count how many indicators we find
                    found_indicators = [ind for ind in expiration_indicators if ind in combined_content]
                    
                    # Check for parking page patterns
                    parking_patterns = [
                        lambda s: bool(re.search(r'domain.*(?:sale|expired|buy)', s)),
                        lambda s: bool(re.search(r'(?:buy|purchase).*domain', s)),
                        lambda s: bool(re.search(r'(?:parked|parking).*domain', s)),
                    ]
                    
                    pattern_matches = [p(combined_content) for p in parking_patterns]
                    
                    # Consider domain expired if we find at least 2 indicators or pattern matches
                    if len(found_indicators) >= 2 or sum(pattern_matches) >= 2:
                        error_msg = f"🕒 Expired domain detected: {domain}\nFound indicators: {', '.join(found_indicators)}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                        continue
                        
                    print(f"✓ URL appears healthy: {domain}")
                        
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

async def wait_until_next_run():
    est = pytz.timezone('US/Eastern')
    now = datetime.now(est)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    
    # If it's already past 10 AM today, schedule for tomorrow
    if now >= target:
        target += timedelta(days=1)
    
    # Calculate wait time
    wait_seconds = (target - now).total_seconds()
    print(f"\nWaiting until {target.strftime('%Y-%m-%d %H:%M:%S %Z')} to run next check")
    await asyncio.sleep(wait_seconds)

async def main():
    print("Starting link checker service...")
    startup_delay = 60  # 1 minute
    print(f"Waiting {startup_delay} seconds for deployment to stabilize...")
    await asyncio.sleep(startup_delay)
    
    print("Service started successfully!")
    send_slack_message("🚀 Link checker service started - Running initial check...")
    
    # Run an immediate check for testing
    print("\nRunning initial URL check...")
    await check_links()
    print("Initial check completed. Switching to daily schedule.")
    send_slack_message("✅ Initial check completed. Now waiting for next scheduled check at 10 AM EST")
    
    # Wait for next 10 AM run
    await wait_until_next_run()
    
    while True:
        print("\nStarting URL check cycle...")
        await check_links()
        await wait_until_next_run()

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