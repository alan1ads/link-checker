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
    return {
        # Definitive expiration messages (these almost always indicate a truly expired domain)
        'definitive': [
            'this domain has expired and is pending renewal or deletion',
            'domain has expired and is pending renewal',
            'this domain expired on',
            'this domain name has expired',
            'domain name registration has expired',
            'this domain name expired on',
            'this domain has expired and is now suspended',
        ],
        # Common registrar expiration pages
        'registrar_patterns': [
            'godaddy.com/expired',
            'expired.namecheap.com',
            'expired.domain',
            'domainexpired',
            'domain-expired',
        ]
    }

def analyze_domain_status(content, domain, response_url, title):
    """
    Analyze domain content to determine if it's truly expired.
    Uses multiple factors including URL, title, and content patterns.
    """
    indicators = get_domain_expiration_indicators()
    
    # Check if we were redirected to a known expiration page
    for pattern in indicators['registrar_patterns']:
        if pattern in response_url.lower():
            return True, f"Redirected to registrar expiration page: {response_url}"
    
    # Look for definitive expiration messages
    for msg in indicators['definitive']:
        if msg in content:
            # Verify the context isn't part of a news article or blog post
            # by checking if it appears in a prominent position
            soup = BeautifulSoup(content, 'html.parser')
            main_content = soup.find('main') or soup.find('body')
            if main_content:
                first_paragraph = main_content.find('p')
                if first_paragraph and msg in first_paragraph.text.lower():
                    return True, f"Found definitive expiration message in main content: {msg}"
    
    # Check for specific expiration patterns that are highly reliable
    expiration_patterns = [
        # Date-based expiration messages
        r'domain\s+expired\s+on\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}',
        r'expiration\s+date:\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}',
        # Registrar-specific patterns
        r'registrar:\s+domain\s+expired',
        r'domain\s+status:\s+expired',
    ]
    
    for pattern in expiration_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            # Extract the matched text for context
            return True, f"Found specific expiration pattern: {match.group(0)}"
    
    # Check page title for definitive indicators
    if title:
        title_lower = title.lower()
        if any(msg in title_lower for msg in indicators['definitive']):
            return True, f"Found expiration message in page title: {title}"
    
    # Check for common expired domain parking pages
    parking_indicators = {
        'title_patterns': [
            'expired domain',
            'domain expired',
            'expired website',
        ],
        'content_patterns': [
            'this domain has expired',
            'renew this domain',
            'domain registration expired',
        ]
    }
    
    # Only consider parking if we see multiple strong indicators
    parking_matches = []
    if title:
        title_lower = title.lower()
        for pattern in parking_indicators['title_patterns']:
            if pattern in title_lower:
                parking_matches.append(f"Title: {pattern}")
    
    for pattern in parking_indicators['content_patterns']:
        if pattern in content:
            parking_matches.append(f"Content: {pattern}")
    
    # Require at least two strong parking indicators
    if len(parking_matches) >= 2:
        return True, f"Multiple parking indicators found: {', '.join(parking_matches)}"
    
    return False, None

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
                print(f"\nChecking URL {checked_count}/{len(domains)}: {domain}")
                
                try:
                    print(f"Making request to: {domain}")
                    response = requests.get(domain, timeout=30, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }, allow_redirects=True)  # Allow redirects to catch expiration pages
                    
                    print(f"Response status code: {response.status_code}")
                    print(f"Final URL after redirects: {response.url}")
                    
                    # Handle different response status codes
                    if response.status_code == 404:
                        print(f"✓ URL returns 404 (acceptable): {domain}")
                        continue
                    elif response.status_code >= 500:
                        error_msg = f"⚠️ Server error for URL {domain}: Status {response.status_code}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                        continue
                    elif response.status_code >= 400:
                        error_msg = f"⚠️ Client error for URL {domain}: Status {response.status_code}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                        continue
                    
                    # Get and parse content
                    print("Getting page content...")
                    response_text = response.text.lower()
                    
                    print("Parsing HTML content...")
                    soup = BeautifulSoup(response_text, 'html.parser')
                    
                    # Get title with proper error handling
                    title = None
                    try:
                        if soup.title and soup.title.string:
                            title = soup.title.string.strip()
                            print(f"Page title: {title}")
                    except Exception as e:
                        print(f"Warning: Error processing title: {str(e)}")
                    
                    # Get visible text content
                    all_text = ''
                    try:
                        all_text = ' '.join([text.lower() for text in soup.stripped_strings])
                    except Exception as e:
                        print(f"Warning: Error processing page text: {str(e)}")
                    
                    # Analyze domain status with the final URL and title
                    is_expired, reason = analyze_domain_status(all_text, domain, response.url, title)
                    
                    if is_expired:
                        error_msg = f"🕒 Expired domain detected: {domain}\n{reason}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    else:
                        print(f"✓ URL appears healthy: {domain}")
                    
                except requests.exceptions.RequestException as e:
                    if isinstance(e, requests.exceptions.SSLError):
                        error_msg = f"⚠️ SSL Certificate error for {domain}"
                    elif isinstance(e, requests.exceptions.ConnectionError):
                        error_msg = f"⚠️ Cannot establish connection to {domain}"
                    elif isinstance(e, requests.exceptions.Timeout):
                        error_msg = f"⚠️ Connection timed out for {domain}"
                    else:
                        error_msg = f"⚠️ Error accessing {domain}: {str(e)}"
                    
                    print(error_msg)
                    failing_domains.append(error_msg)
                except Exception as e:
                    error_msg = f"❌ Unexpected error checking {domain}: {str(e)}"
                    print(error_msg)
                    failing_domains.append(error_msg)
            
            print(f"\nChecked {checked_count} URLs")
            
            if failing_domains:
                print("\nSending notifications for failing domains...")
                batch_size = 20
                for i in range(0, len(failing_domains), batch_size):
                    batch = failing_domains[i:i + batch_size]
                    message = "URL Check Results:\n" + "\n".join(batch)
                    send_slack_message(message)
            else:
                print("\nAll URLs are healthy")
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
        error_msg = f"Critical error in check_links: {str(e)}"
        print(error_msg)
        send_slack_message(f"❌ {error_msg}")

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