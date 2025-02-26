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
        # Print raw HTML for debugging
        print("\n=== DEBUG: Raw HTML ===")
        print(content[:2000])  # Show more content for debugging
        
        # Parse with both html.parser and lxml to catch more content
        soup = BeautifulSoup(content, 'html.parser')
        
        # Extract text from multiple sources
        text_sources = []
        
        # Get all text content, including hidden elements
        all_text = soup.get_text(separator=' ', strip=True).lower()
        text_sources.append(all_text)
        print(f"\n=== DEBUG: All text content ===\n{all_text[:500]}")
        
        # Look for specific span with style attributes (common in expired domain pages)
        for span in soup.find_all('span', style=True):
            if 'font-family:Arial' in span.get('style', ''):
                text = span.get_text(strip=True).lower()
                text_sources.append(text)
                print(f"Found styled span text: {text}")
        
        # Check for specific elements that might contain the expiration message
        for tag in soup.find_all(['span', 'div', 'p', 'h1', 'h2', 'h3']):
            if tag.get('style') or tag.get('class'):
                text = tag.get_text(strip=True).lower()
                text_sources.append(text)
                print(f"Found styled element text: {text}")
        
        # Combine all text sources
        full_text = ' '.join(text_sources)
        
        # Look for the exact expiration message pattern
        exact_pattern = "the domain has expired. is this your domain?"
        if exact_pattern in full_text.lower():
            print(f"Found exact expiration message!")
            return True, "Found exact domain expiration message"
        
        # Look for variations of the expiration message
        expiration_patterns = [
            "domain has expired",
            "this domain has expired",
            "the domain has expired",
            "renew now",
            "domain renewal"
        ]
        
        for pattern in expiration_patterns:
            if pattern in full_text.lower():
                context_start = max(0, full_text.lower().find(pattern) - 50)
                context_end = min(len(full_text), full_text.lower().find(pattern) + len(pattern) + 50)
                context = full_text[context_start:context_end]
                print(f"Found expiration pattern: {pattern}")
                print(f"Context: {context}")
                return True, f"Found expiration message: {context}"
        
        return False, None
        
    except Exception as e:
        print(f"Error in analyze_domain_status: {str(e)}")
        return False, None

async def check_links():
    try:
        print("Attempting to connect to Google Sheet...")
        
        try:
            spreadsheet = creds.open_by_key(SHEET_URL)
            sheet = next((ws for ws in spreadsheet.worksheets() if ws.id == 0), None)
            if not sheet:
                raise Exception("Could not find worksheet")
            
            all_values = sheet.get_all_values()
            domains = [row[2].strip() for row in all_values[1:] if len(row) > 2 and row[2].strip()]
            domains = ['http://' + d if not d.startswith(('http://', 'https://')) else d for d in domains]
            
            failing_domains = []
            checked_count = 0
            
            for domain in domains:
                checked_count += 1
                print(f"\n{'='*50}")
                print(f"Checking URL {checked_count}/{len(domains)}: {domain}")
                print(f"{'='*50}")
                
                try:
                    response = requests.get(domain, timeout=30, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }, allow_redirects=True)
                    
                    print(f"Response status code: {response.status_code}")
                    print(f"Final URL after redirects: {response.url}")
                    
                    # Only analyze content for 200 responses or 403s
                    if response.status_code == 200 or response.status_code == 403:
                        is_expired, reason = analyze_domain_status(response.text, domain, response.url, None)
                        if is_expired:
                            error_msg = f"🕒 Expired domain detected: {domain}\n{reason}"
                            failing_domains.append(error_msg)
                            print(error_msg)
                        else:
                            print(f"✓ URL appears healthy: {domain}")
                    elif response.status_code != 404:  # Only exclude 404s from reporting
                        error_msg = f"⚠️ HTTP {response.status_code} error for {domain}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    else:
                        print(f"404 error for {domain} - not reporting to Slack")
                    
                except requests.exceptions.RequestException as e:
                    error_msg = f"⚠️ Error accessing {domain}: {str(e)}"
                    failing_domains.append(error_msg)
                    print(error_msg)
                except Exception as e:
                    error_msg = f"❌ Unexpected error checking {domain}: {str(e)}"
                    failing_domains.append(error_msg)
                    print(error_msg)
            
            # Send notifications only for real issues
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