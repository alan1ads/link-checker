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
    Enhanced version to handle various page structures and content loading methods.
    """
    try:
        # Parse the content with different parsers to ensure we catch everything
        soup = BeautifulSoup(content, 'html.parser')
        
        # Extract text from multiple sources
        text_sources = []
        
        # 1. Get text from standard tags with better structure handling
        for tag in soup.find_all(['p', 'div', 'span', 'h1', 'h2', 'h3', 'title', 'a', 'button']):
            text = tag.get_text(strip=True)
            if text:
                text_sources.append(text.lower())
                print(f"Standard tag text: {text[:200]}")
                
                # Check href for renewal links
                if tag.name == 'a' and tag.get('href'):
                    href = tag.get('href').lower()
                    if any(domain in href for domain in ['namecheap.com', 'godaddy.com', 'renew', 'restore']):
                        text_sources.append(f"renewal link found: {href}")
        
        # 2. Check iframes with better error handling
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '')
            if src:
                print(f"Found iframe with src: {src}")
                try:
                    iframe_response = requests.get(src, timeout=10)
                    iframe_soup = BeautifulSoup(iframe_response.text, 'html.parser')
                    iframe_text = iframe_soup.get_text(strip=True)
                    text_sources.append(iframe_text.lower())
                    print(f"Iframe content: {iframe_text[:200]}")
                except Exception as e:
                    print(f"Could not fetch iframe content: {e}")
        
        # 3. Check for text in attributes with expanded attribute list
        for tag in soup.find_all(True):
            for attr in ['data-content', 'aria-label', 'title', 'alt', 'placeholder', 'data-text']:
                if attr_text := tag.get(attr):
                    text_sources.append(attr_text.lower())
                    print(f"Attribute text ({attr}): {attr_text}")
        
        # Combine all text sources
        full_text = ' '.join(text_sources)
        
        # Define comprehensive patterns to check (case insensitive)
        expiration_patterns = [
            r"domain has expired",
            r"this domain has expired",
            r"the domain has expired",
            r"domain is expired",
            r"expired domain",
            r"domain name has expired",
            r"renew now",
            r"domain renewal",
            r"domain expiration",
            r"domain not found",
            r"domain doesn't exist",
            r"domain does not exist",
            r"domain registration expired",
            r"this domain is not active"
        ]
        
        # Check for registrar-specific patterns
        registrar_patterns = [
            r"namecheap\.com/renew",
            r"godaddy\.com/renew",
            r"expired\.domains",
            r"domain\.com/renew",
            r"restore-domain"
        ]
        
        # First check for exact expiration message
        if "the domain has expired. is this your domain?" in full_text.lower():
            return True, "Found exact domain expiration message"
        
        # Then check for pattern combinations
        for pattern in expiration_patterns:
            matches = re.finditer(pattern, full_text, re.I)
            for match in matches:
                # Get context around the match
                start = max(0, match.start() - 100)
                end = min(len(full_text), match.end() + 100)
                context = full_text[start:end]
                
                # Check if there's a registrar pattern near the match
                for reg_pattern in registrar_patterns:
                    if re.search(reg_pattern, full_text, re.I):
                        return True, f"Found expiration message with registrar reference: {context}"
                
                # If we found a strong expiration message, return true
                if any(strong_pattern in pattern.lower() for strong_pattern in [
                    "domain has expired",
                    "the domain has expired",
                    "domain is expired"
                ]):
                    return True, f"Found strong expiration message: {context}"
                
                # For weaker patterns, look for supporting evidence
                if "renew" in full_text.lower() or "restore" in full_text.lower():
                    return True, f"Found expiration message with renewal option: {context}"
        
        return False, None
        
    except Exception as e:
        print(f"Error in analyze_domain_status: {str(e)}")
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
                    
                    # Handle different status codes
                    if response.status_code >= 400:
                        error_type = "🔒" if response.status_code in [401, 403, 407] else "⚠️"
                        error_msg = f"{error_type} HTTP {response.status_code} error for {domain}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                        
                        # Try to analyze content even for error responses
                        try:
                            is_expired, reason = analyze_domain_status(response.text, domain, response.url, None)
                            if is_expired:
                                error_msg = f"🕒 Expired domain detected in error response: {domain}\n{reason}"
                                failing_domains.append(error_msg)
                                print(error_msg)
                        except Exception as e:
                            print(f"Could not analyze error response content: {str(e)}")
                        continue
                    
                    # Analyze domain status for successful responses
                    is_expired, reason = analyze_domain_status(response.text, domain, response.url, None)
                    if is_expired:
                        error_msg = f"🕒 Expired domain detected: {domain}\n{reason}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    else:
                        print(f"✓ URL appears healthy: {domain}")
                    
                except requests.exceptions.RequestException as e:
                    error_msg = f"⚠️ Error accessing {domain}: {str(e)}"
                    print(error_msg)
                    failing_domains.append(error_msg)
                except Exception as e:
                    error_msg = f"❌ Unexpected error checking {domain}: {str(e)}"
                    print(error_msg)
                    failing_domains.append(error_msg)
            
            # Send notifications in batches
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
                
        except Exception as e:
            error_msg = f"Error in sheet processing: {str(e)}"
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