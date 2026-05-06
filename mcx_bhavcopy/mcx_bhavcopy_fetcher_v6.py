import os
import time
import json
import csv
import logging
from datetime import datetime, date
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Configuration
COMMODITIES = ["GOLD", "GOLDM", "SILVER", "SILVERM", "SILVERMIC", "NATURALGAS", "NATGASMINI"]
INSTRUMENT = "FUTCOM"
# Start date for initial fetch as requested by user
START_DATE = "01/04/2026" 
OUTPUT_DIR = r"c:\Users\Admin\OneDrive\Swap Data\Papertrading\mcx_bhavcopy\data"

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "mcx_fetcher.log")),
        logging.StreamHandler()
    ]
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

def switch_to_commodity_wise(driver):
    """Switches the mode from Date Wise to Commodity Wise."""
    try:
        # The subagent found a toggle arrow near (203, 343)
        # Usually, it's a div or span with a specific class for the toggle
        toggle = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".mode-toggle, .toggle-arrow, .toggle")) # Guessed classes
        )
        toggle.click()
    except:
        # Fallback to pixel click if ID/Class is not found (though less portable)
        # Better: search for the text and click the sibling
        try:
            label = driver.find_element(By.XPATH, "//span[contains(text(), 'Date Wise')]")
            # Click near the label
            from selenium.webdriver.common.action_chains import ActionChains
            actions = ActionChains(driver)
            actions.move_to_element_with_offset(label, 100, 0).click().perform()
        except Exception as e:
            logger.error(f"Failed to switch mode: {e}")
            return False
    
    time.sleep(2)
    return True

def fetch_data_for_commodity(driver, commodity, start_date, end_date):
    logger.info(f"Fetching data for {commodity} from {start_date} to {end_date}...")
    
    try:
        # 1. Select Instrument
        instrument_dropdown = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ddlInstrument"))
        )
        Select(instrument_dropdown).select_by_value(INSTRUMENT)
        time.sleep(1)
        
        # 2. Select Commodity (RadComboBox)
        # We'll use Javascript to set the value of the Telerik control
        driver.execute_script(f"""
            var combo = $find("ddlSymbols");
            if (combo) {{
                var item = combo.findItemByText("{commodity}");
                if (item) {{
                    item.select();
                }} else {{
                    combo.set_text("{commodity}");
                }}
            }}
        """)
        time.sleep(2)
        
        # 3. Select Expiry
        # We'll select the first available expiry for now, as usually the latest one is the default
        expiry_dropdown = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ddlExpiry"))
        )
        # Select first option that is not "Select Expiry"
        select = Select(expiry_dropdown)
        if len(select.options) > 1:
            select.select_by_index(1)
        
        # 4. Set Dates
        driver.execute_script(f"document.getElementById('txtFromDate').value = '{start_date}';")
        driver.execute_script(f"document.getElementById('txtToDate').value = '{end_date}';")
        
        # 5. Click Show
        show_btn = driver.find_element(By.ID, "btnShowCommoditywise")
        driver.execute_script("arguments[0].click();", show_btn)
        
        # Wait for data to load
        time.sleep(5)
        
        # 6. Extract vBC data
        data = driver.execute_script("return vBC;")
        return data
        
    except Exception as e:
        logger.error(f"Error fetching {commodity}: {e}")
        return None

def save_to_csv(commodity, data):
    if not data:
        logger.warning(f"No data to save for {commodity}")
        return
    
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    file_path = os.path.join(OUTPUT_DIR, f"{commodity.lower()}_ohlc.csv")
    
    # Headers based on MCX vBC structure
    # Expected fields: Date, Open, High, Low, Close, Volume, OI, etc.
    # We'll extract keys from the first record
    headers = data[0].keys()
    
    with open(file_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
    
    logger.info(f"Successfully saved {len(data)} rows for {commodity} to {file_path}")

def main():
    driver = setup_driver()
    try:
        driver.get("https://www.mcxindia.com/market-data/bhavcopy")
        time.sleep(5)
        
        if not switch_to_commodity_wise(driver):
            logger.error("Could not switch to Commodity Wise mode. Exiting.")
            return
            
        today_str = date.today().strftime("%d/%m/%Y")
        
        for commodity in COMMODITIES:
            # We fetch from START_DATE to today to ensure we have the requested history
            data = fetch_data_for_commodity(driver, commodity, START_DATE, today_str)
            if data:
                # Filter data if needed (e.g. only specific expiry)
                # But vBC already contains the filtered results from the UI
                save_to_csv(commodity, data)
            else:
                logger.error(f"Failed to get data for {commodity}")
                
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
