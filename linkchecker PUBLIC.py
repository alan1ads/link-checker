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
    Uses multiple approaches to detect expired domains.
    """
    # Parse the content once
    soup = BeautifulSoup(content, 'html.parser')
    
    # Get all text content, including hidden elements
    all_text = ' '.join([text.lower() for text in soup.stripped_strings])
    
    # 1. Check for standard expiration messages
    expiration_phrases = [
        'domain has expired',
        'domain expired',
        'domain name expired',
        'expired domain',
        'this domain is expired',
        'domain registration expired',
    ]
    
    # 2. Look for common page structures that indicate expiration
    def check_page_structure():
        # Look for short pages with minimal content (typical of expired domains)
        main_content = soup.find('body')
        if main_content:
            text_content = main_content.get_text(strip=True).lower()
            # If page is suspiciously short and contains key terms
            if len(text_content) < 1000 and ('domain' in text_content and 'expired' in text_content):
                return True
        return False
    
    # 3. Check for renewal/restoration links or buttons
    def check_renewal_elements():
        renewal_terms = ['renew', 'restore', 'reactivate', 'purchase']
        
        # Check link texts
        links = soup.find_all('a')
        link_texts = [link.get_text().lower().strip() for link in links]
        
        # Check button texts
        buttons = soup.find_all(['button', 'input'])
        button_texts = [btn.get('value', '').lower() for btn in buttons]
        button_texts.extend([btn.get_text().lower().strip() for btn in buttons])
        
        # Combine all interactive elements
        all_elements = link_texts + button_texts
        return any(any(term in element for term in renewal_terms) for element in all_elements)
    
    # 4. Check for typical expired domain page layout
    def check_page_layout():
        # Look for centered text containers (common in expired pages)
        centered_divs = soup.find_all('div', style=lambda s: s and ('center' in s.lower() or 'margin: auto' in s.lower()))
        for div in centered_divs:
            text = div.get_text().lower()
            if 'domain' in text and ('expired' in text or 'renew' in text):
                return True
        return False
    
    # 5. Check for registrar-specific patterns
    def check_registrar_patterns():
        registrar_indicators = {
            'godaddy': ['godaddy', 'domain expired on godaddy'],
            'namecheap': ['namecheap', 'expired.namecheap'],
            'name.com': ['name.com', 'expired.name.com'],
            'network solutions': ['networksolutions', 'renew.web.com'],
            'enom': ['enom', 'domainexpired.com'],
        }
        
        for registrar, patterns in registrar_indicators.items():
            if any(pattern in response_url.lower() for pattern in patterns):
                return True, f"Detected {registrar} expiration page"
        return False, None
    
    # 6. Check for common expired domain redirects
    def check_redirects():
        redirect_patterns = [
            'expired.domain',
            'domainexpired',
            'domain-expired',
            'expireddomains',
            'domain.pending',
        ]
        return any(pattern in response_url.lower() for pattern in redirect_patterns)
    
    # Combine all checks
    reasons = []
    
    # Check registrar patterns first
    is_registrar, registrar_reason = check_registrar_patterns()
    if is_registrar:
        reasons.append(registrar_reason)
    
    # Check for expiration phrases in main content
    found_phrases = [phrase for phrase in expiration_phrases if phrase in all_text]
    if found_phrases:
        reasons.append(f"Found expiration phrases: {', '.join(found_phrases)}")
    
    # Check page structure
    if check_page_structure():
        reasons.append("Page structure indicates expired domain")
    
    # Check for renewal elements
    if check_renewal_elements():
        reasons.append("Found renewal/restoration elements")
    
    # Check page layout
    if check_page_layout():
        reasons.append("Page layout matches expired domain pattern")
    
    # Check redirects
    if check_redirects():
        reasons.append("Domain redirects to expiration page")
    
    # Make final decision
    # If we have multiple indicators or a registrar pattern, consider it expired
    if len(reasons) >= 2 or is_registrar:
        return True, "\n".join(reasons)
    
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