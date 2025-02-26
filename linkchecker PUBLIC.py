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
        # Exact patterns from common expired domain pages
        'exact_patterns': [
            'the domain has expired. is this your domain?',
            'the domain has expired. is this your domain? renew now',
            'domain has expired. renew now',
            'the domain has expired.',
        ],
        # Common registrar expiration pages
        'registrar_patterns': [
            'godaddy.com/expired',
            'expired.namecheap.com',
            'expired.domain',
            'domainexpired',
            'domain-expired',
        ],
        # Link text patterns that often appear with expired domains
        'link_patterns': [
            'renew now',
            'renew domain',
            'restore domain',
            'reactivate domain'
        ]
    }

def analyze_domain_status(content, domain, response_url, title):
    """
    Analyze domain content to determine if it's truly expired.
    Focuses on actual content visible in the webpage.
    """
    # Parse the content once
    soup = BeautifulSoup(content, 'html.parser')
    
    # DEBUG: Print raw HTML to see what we're getting
    print("\n=== DEBUG: Raw HTML snippet ===")
    print(content[:500])  # First 500 chars of raw HTML
    
    # DEBUG: Print parsed structure
    print("\n=== DEBUG: Page Structure ===")
    for tag in soup.find_all(['title', 'h1', 'h2', 'p', 'div']):
        print(f"{tag.name}: {tag.get_text().strip()[:100]}")
    
    # Get all visible text from the page, preserving some structure
    all_text = []
    for element in soup.stripped_strings:
        text = element.lower().strip()
        if text:  # Only add non-empty strings
            all_text.append(text)
            # DEBUG: Print each text element we find
            print(f"Found text: {text[:100]}")
    
    # Join all text pieces, preserving their original separation
    full_text = ' '.join(all_text)
    print(f"\n=== DEBUG: Full processed text for {domain} ===")
    print(full_text)
    
    # Look for the exact expiration message pattern
    if "the domain has expired. is this your domain?" in full_text:
        print("Found exact expiration message!")
        return True, "Found standard domain expiration message"
    
    # Look for variations of the expiration message
    expiration_patterns = [
        "domain has expired",
        "this domain has expired",
        "domain is expired",
        "expired domain",
        "domain name has expired"
    ]
    
    # Check each pattern
    for pattern in expiration_patterns:
        if pattern in full_text:
            # When we find a potential match, look at the surrounding context
            # Find the full sentence or section containing this pattern
            words = full_text.split()
            for i, word in enumerate(words):
                if pattern in ' '.join(words[i:i+len(pattern.split())]):
                    # Get some context (10 words before and after for better context)
                    start = max(0, i-10)
                    end = min(len(words), i+len(pattern.split())+10)
                    context = ' '.join(words[start:end])
                    print(f"\n=== DEBUG: Found expiration pattern with context ===")
                    print(f"Pattern: {pattern}")
                    print(f"Context: {context}")
                    return True, f"Found expiration message: {context}"
    
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
                print(f"\n{'='*50}")
                print(f"Checking URL {checked_count}/{len(domains)}: {domain}")
                print(f"{'='*50}")
                
                try:
                    print(f"Making request to: {domain}")
                    response = requests.get(domain, timeout=30, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }, allow_redirects=True)
                    
                    print(f"Response status code: {response.status_code}")
                    print(f"Final URL after redirects: {response.url}")
                    print(f"Content length: {len(response.text)} characters")
                    
                    # Get and parse content
                    print("\nGetting page content...")
                    response_text = response.text
                    
                    print("Parsing HTML content...")
                    soup = BeautifulSoup(response_text, 'html.parser')
                    
                    # DEBUG: Print encoding information
                    print(f"Response encoding: {response.encoding}")
                    print(f"Content type: {response.headers.get('content-type', 'unknown')}")
                    
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