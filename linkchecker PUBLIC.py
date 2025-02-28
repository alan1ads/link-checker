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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

def setup_selenium():
    chrome_options = Options()
    chrome_options.add_argument('--headless=new')  # New headless mode
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--ignore-certificate-errors')
    chrome_options.add_argument('--disable-http2')  # Disable HTTP/2 to avoid protocol errors
    chrome_options.add_argument('--disable-javascript-harmony-shipping')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    # Add experimental options
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    return webdriver.Chrome(options=chrome_options)

def analyze_domain_status(content, domain, response_url, title, driver=None):
    """
    Analyze domain content to determine if it's truly expired.
    Checks for various common expiration message patterns.
    """
    try:
        # If we have a Selenium driver, get the JavaScript-rendered content
        if driver:
            try:
                print("\n=== Checking for domain expiration ===")
                driver.get(domain)
                
                # Wait for body to load
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Wait a moment for dynamic content
                time.sleep(5)  # Increased wait time for iframe load
                
                try:
                    # First make the target div visible
                    driver.execute_script("""
                        var target = document.getElementById('target');
                        if (target) {
                            target.style.opacity = '1';
                            target.style.visibility = 'visible';
                            target.style.display = 'block';
                        }
                    """)
                    
                    # Look specifically for plFrame
                    try:
                        iframe = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.ID, "plFrame"))
                        )
                        print("Found plFrame iframe")
                        
                        # Switch to the iframe
                        driver.switch_to.frame(iframe)
                        
                        # Wait for and get the content
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.TAG_NAME, "span"))
                        )
                        
                        # Get all spans and their text
                        spans = driver.find_elements(By.TAG_NAME, "span")
                        for span in spans:
                            try:
                                text = span.text.strip().lower()
                                print(f"Found text in plFrame: {text}")
                                if "domain has expired" in text:
                                    driver.switch_to.default_content()
                                    return True, f"Found expired domain message: {text}"
                            except Exception as e:
                                print(f"Error reading span text: {e}")
                                continue
                        
                        driver.switch_to.default_content()
                    except Exception as e:
                        print(f"Error with plFrame: {e}")
                        driver.switch_to.default_content()
                
                except Exception as e:
                    print(f"Error making target visible: {e}")
                
                # Keep all existing checks (they're working for other cases)
                page_text = driver.page_source.lower()
                
                # Common expiration message patterns (keeping existing ones that work)
                expiration_patterns = [
                    # Exact matches from screenshot
                    "the domain has expired. is this your domain?",
                    "the domain has expired. is this your domain? renew now",
                    "domain has expired. renew now",
                    
                    # Common variations that were working
                    "this domain has expired",
                    "domain name has expired",
                    "domain registration has expired",
                    "domain expired",
                    "expired domain",
                    "domain is expired",
                    "domain has lapsed",
                    "domain registration expired",
                    "this domain is expired",
                    "this domain name has expired",
                    "domain has been expired",
                    "domain registration has lapsed",
                    "domain has expired and is pending renewal",
                    "expired domain name",
                    "domain expiration notice"
                ]
                
                # Check for patterns in the page source
                for pattern in expiration_patterns:
                    if pattern in page_text:
                        print(f"Found expiration message: {pattern}")
                        return True, f"Found domain expiration message: {pattern}"
                
                # Keep existing span checks that were working
                span_selectors = [
                    "span[style*='font-family:Arial']",
                    "span[style*='font-size']",
                    "span.expired-domain",
                    "span.domain-expired",
                    "div.expired-notice"
                ]
                
                for selector in span_selectors:
                    spans = driver.find_elements(By.CSS_SELECTOR, selector)
                    for span in spans:
                        text = span.text.strip().lower()
                        for pattern in expiration_patterns:
                            if pattern in text:
                                print(f"Found expiration message in styled element: {text}")
                                return True, f"Found domain expiration message: {text}"
                
            except Exception as e:
                print(f"Error checking JavaScript content: {e}")
        
        return False, None
        
    except Exception as e:
        print(f"Error in analyze_domain_status: {str(e)}")
        return False, None

async def check_links():
    try:
        print("Setting up Selenium...")
        driver = setup_selenium()
        
        print("Attempting to connect to Google Sheet...")
        try:
            spreadsheet = creds.open_by_key(SHEET_URL)
            sheet = next((ws for ws in spreadsheet.worksheets() if ws.id == 0), None)
            if not sheet:
                raise Exception("Could not find worksheet")
            
            all_values = sheet.get_all_values()
            # Get both Ad Account Name and domain, skip header row
            domain_data = [(row[0].strip(), row[2].strip()) for row in all_values[1:] if len(row) > 2 and row[2].strip()]
            # Add http:// if needed and keep the account name
            domain_data = [(account, 'http://' + domain if not domain.startswith(('http://', 'https://')) else domain) 
                          for account, domain in domain_data]
            
            # Limit to first 50 domains for testing
            domain_data = domain_data[:50]
            
            failing_domains = []
            checked_count = 0
            
            for account_name, domain in domain_data:
                checked_count += 1
                print(f"\n{'='*50}")
                print(f"Checking URL {checked_count}/{len(domain_data)}: {domain}")
                print(f"Ad Account: {account_name}")
                print(f"{'='*50}")
                
                try:
                    response = requests.get(domain, timeout=30, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }, allow_redirects=True)
                    
                    print(f"Response status code: {response.status_code}")
                    print(f"Final URL after redirects: {response.url}")
                    
                    if response.status_code == 200:
                        is_expired, reason = analyze_domain_status(response.text, domain, response.url, None, driver)
                        if is_expired:
                            error_msg = f"🚫 Domain expired: {domain} / {account_name}"
                            failing_domains.append(error_msg)
                            print(error_msg)
                        else:
                            print(f"✓ URL appears healthy: {domain}")
                    elif response.status_code == 403:
                        error_msg = f"🔒 Access Forbidden (403): {domain} / {account_name}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    elif response.status_code != 404:  # Only exclude 404s from reporting
                        error_msg = f"⚠️ HTTP {response.status_code}: {domain} / {account_name}"
                        failing_domains.append(error_msg)
                        print(error_msg)
                    else:
                        print(f"404 error for {domain} - not reporting to Slack")
                    
                except requests.exceptions.RequestException as e:
                    error_msg = f"❌ Connection Error: {domain} / {account_name}"
                    failing_domains.append(error_msg)
                    print(error_msg)
                except Exception as e:
                    error_msg = f"❌ Unexpected Error: {domain} / {account_name}"
                    failing_domains.append(error_msg)
                    print(error_msg)
            
            if failing_domains:
                print("\nSending notifications for failing domains...")
                message = "🔍 Link Check Results:\n" + "\n".join(failing_domains)
                send_slack_message(message)
            else:
                print("\nAll URLs are healthy")
                send_slack_message("✅ All URLs are functioning correctly")
                
        finally:
            print("Closing Selenium browser...")
            driver.quit()
                
    except Exception as e:
        error_msg = f"⚠️ Critical error: {str(e)}"
        print(error_msg)
        send_slack_message(error_msg)

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