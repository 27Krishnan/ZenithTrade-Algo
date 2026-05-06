import os
import time
import csv
import logging
from datetime import datetime, date, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURATION ---
COMMODITY_CONFIG = {
    "GOLD":        {"expiry": "05JUN2026"},
    "GOLDM":       {"expiry": "05JUN2026"},
    "SILVER":      {"expiry": "03JUL2026"},
    "SILVERM":     {"expiry": "30JUN2026"},
    "SILVERMIC":   {"expiry": "30JUN2026"},
    "NATURALGAS":  {"expiry": "26MAY2026"},
    "NATGASMINI":  {"expiry": "26MAY2026"},
    "ALUMINIUM":   {"expiry": "29MAY2026"},
}

DATA_DIR = r"c:\Users\Admin\OneDrive\Swap Data\Papertrading\data\mcx_ohlc"
LOG_FILE = os.path.join(os.path.dirname(__file__), "mcx_fetcher_production.log")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("window-size=1920,1080")
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

def get_last_date(file_path):
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, 'r') as f:
            reader = csv.reader(f)
            header = next(reader)
            dates = []
            for row in reader:
                if row:
                    dates.append(datetime.strptime(row[0], "%d %b %Y").date())
            return max(dates) if dates else None
    except Exception as e:
        logger.error(f"Error reading dates from {file_path}: {e}")
        return None

def fetch_data(driver, commodity, expiry, from_date, to_date):
    logger.info(f"Procedure: Fetching {commodity} ({expiry}) from {from_date} to {to_date}")
    
    try:
        # STEP 1: OPEN PAGE
        driver.get("https://www.mcxindia.com/market-data/bhavcopy")
        time.sleep(5)
        
        # STEP 2: SWITCH MODE
        mode_toggle = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//label[contains(text(), 'Commodity Wise')]"))
        )
        mode_toggle.click()
        time.sleep(2)
        
        # STEP 3: APPLY FILTERS
        # Instrument
        instrument_select = Select(driver.find_element(By.ID, "ddlInstrument"))
        instrument_select.select_by_value("FUTCOM")
        time.sleep(2)
        
        # Commodity (via JS for RadComboBox)
        driver.execute_script(f"""
            var combo = $find("ddlSymbols");
            if (combo) {{
                var item = combo.findItemByText("{commodity}");
                if (item) {{ item.select(); }} else {{ combo.set_text("{commodity}"); }}
            }}
        """)
        time.sleep(2)
        
        # Expiry - Retry until populated
        expiry_selected = False
        for _ in range(5):
            try:
                expiry_select = Select(driver.find_element(By.ID, "ddlExpiry"))
                expiry_select.select_by_visible_text(expiry)
                expiry_selected = True
                break
            except:
                logger.info(f"Retrying expiry selection for {commodity}...")
                time.sleep(2)
        
        if not expiry_selected:
            # Try partial match or just first valid one
            try:
                expiry_select = Select(driver.find_element(By.ID, "ddlExpiry"))
                if len(expiry_select.options) > 1:
                    expiry_select.select_by_index(1)
                    logger.warning(f"Selected fallback expiry for {commodity}")
                else:
                    raise Exception("No expiries found")
            except Exception as e:
                logger.error(f"Expiry selection failed for {commodity}: {e}")
                return None
        
        # Dates
        driver.execute_script(f"document.getElementById('txtFromDate').value = '{from_date}';")
        driver.execute_script(f"document.getElementById('txtToDate').value = '{to_date}';")
        
        # STEP 4: CLICK SHOW
        show_btn = driver.find_element(By.ID, "btnShowCommoditywise")
        driver.execute_script("arguments[0].click();", show_btn)
        time.sleep(5)
        
        # STEP 5 & 6: EXTRACTION & PAGINATION
        all_rows = []
        
        def extract():
            rows = []
            try:
                table = driver.find_element(By.ID, "tblBhavCopy")
                trs = table.find_elements(By.TAG_NAME, "tr")
                for tr in trs[1:]:
                    tds = tr.find_elements(By.TAG_NAME, "td")
                    if len(tds) >= 13:
                        rows.append([
                            tds[0].text, tds[5].text, tds[6].text, tds[7].text, 
                            tds[8].text, tds[10].text, tds[12].text
                        ])
            except: pass
            return rows

        all_rows.extend(extract())
        
        # Check for Page 2
        try:
            p2 = driver.find_element(By.LINK_TEXT, "2")
            p2.click()
            time.sleep(3)
            all_rows.extend(extract())
        except: pass
        
        return all_rows

    except Exception as e:
        logger.error(f"Failed to fetch {commodity}: {e}")
        return None

def update_csv(commodity, data):
    if not data: return
    
    file_path = os.path.join(DATA_DIR, f"{commodity.lower()}_ohlc.csv")
    existing_data = {}
    
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            reader = csv.reader(f)
            header = next(reader)
            for row in reader:
                existing_data[row[0]] = row
    else:
        header = ["Date", "Open", "High", "Low", "Close", "Volume", "OI"]
    
    for row in data:
        existing_data[row[0]] = row # Overwrite/Add
        
    # Sort by date
    sorted_rows = sorted(
        existing_data.values(), 
        key=lambda x: datetime.strptime(x[0], "%d %b %Y"),
        reverse=True
    )
    
    with open(file_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(sorted_rows)
    
    logger.info(f"Updated {file_path} with {len(data)} fresh records.")

def main():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    
    driver = setup_driver()
    today = date.today()
    to_date_str = today.strftime("%d/%m/%Y")
    
    try:
        for commodity, config in COMMODITY_CONFIG.items():
            file_path = os.path.join(DATA_DIR, f"{commodity.lower()}_ohlc.csv")
            last_date = get_last_date(file_path)
            
            if last_date and last_date >= today:
                logger.info(f"Data for {commodity} is already up to date ({last_date})")
                continue
                
            from_date = (last_date + timedelta(days=1)) if last_date else date(2026, 4, 1)
            from_date_str = from_date.strftime("%d/%m/%Y")
            
            # Use strict procedure
            data = fetch_data(driver, commodity, config['expiry'], from_date_str, to_date_str)
            if data:
                update_csv(commodity, data)
                
    finally:
        driver.quit()
        logger.info("Daily Fetching Cycle Complete.")

if __name__ == "__main__":
    main()
