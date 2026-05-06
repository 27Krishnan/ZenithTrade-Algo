import os
import time
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

def fetch_mcx_bhavcopy(commodity_name, expiry_name, from_date, to_date):
    """
    Strictly follows the 7-step procedure provided by the user.
    """
    driver = setup_driver()
    data_rows = []
    
    try:
        # STEP 1: OPEN PAGE
        logger.info("Opening MCX Bhavcopy page...")
        driver.get("https://www.mcxindia.com/market-data/bhavcopy")
        time.sleep(5)
        
        # STEP 2: SWITCH MODE
        logger.info("Switching to Commodity Wise mode...")
        # The toggle arrow next to "Date Wise"
        try:
            # Finding the arrow or label for Commodity Wise
            mode_label = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//label[contains(text(), 'Commodity Wise')]"))
            )
            mode_label.click()
            time.sleep(2)
        except Exception as e:
            logger.error(f"Failed to switch mode: {e}")
            return None

        # STEP 3: APPLY FILTERS (STRICT)
        logger.info(f"Applying filters for {commodity_name}...")
        
        # Instrument
        instrument_select = Select(driver.find_element(By.ID, "ddlInstrument"))
        instrument_select.select_by_value(INSTRUMENT)
        time.sleep(2)
        
        # Commodity (RadComboBox - setting via JS for reliability)
        driver.execute_script(f"""
            var combo = $find("ddlSymbols");
            if (combo) {{
                var item = combo.findItemByText("{commodity_name}");
                if (item) {{
                    item.select();
                }} else {{
                    combo.set_text("{commodity_name}");
                }}
            }}
        """)
        time.sleep(2)
        
        # Expiry
        expiry_select = Select(driver.find_element(By.ID, "ddlExpiry"))
        expiry_select.select_by_visible_text(expiry_name)
        time.sleep(1)
        
        # Start Date and End Date
        driver.execute_script(f"document.getElementById('txtFromDate').value = '{from_date}';")
        driver.execute_script(f"document.getElementById('txtToDate').value = '{to_date}';")
        
        # STEP 4: CLICK SHOW
        logger.info("Clicking Show...")
        show_btn = driver.find_element(By.ID, "btnShowCommoditywise")
        driver.execute_script("arguments[0].click();", show_btn)
        time.sleep(5)
        
        # STEP 5 & 6: DATA EXTRACTION & FILTERING
        # Option B: Read table manually (Page 1 and Page 2)
        def extract_table_rows():
            rows = []
            table = driver.find_element(By.ID, "tblBhavCopy") # Check the ID from screenshot/DOM
            # Wait for data rows
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "tr")))
            for tr in table.find_elements(By.TAG_NAME, "tr")[1:]: # Skip header
                tds = tr.find_elements(By.TAG_NAME, "td")
                if len(tds) >= 10:
                    row = {
                        "Date": tds[0].text,
                        "Open": tds[5].text,
                        "High": tds[6].text,
                        "Low": tds[7].text,
                        "Close": tds[8].text,
                        "Volume": tds[10].text,
                        "OI": tds[12].text
                    }
                    rows.append(row)
            return rows

        logger.info("Extracting Page 1...")
        data_rows.extend(extract_table_rows())
        
        # Go to Page 2
        try:
            next_page = driver.find_element(By.LINK_TEXT, "2")
            next_page.click()
            time.sleep(3)
            logger.info("Extracting Page 2...")
            data_rows.extend(extract_table_rows())
        except:
            logger.info("Page 2 not found or not needed.")

    except Exception as e:
        logger.error(f"Procedure failed: {e}")
        return None
    finally:
        driver.quit()
        
    return data_rows

def main():
    # Example for SILVERM as requested
    commodity = "SILVERM"
    expiry = "30JUN2026"
    from_date = "01/04/2026"
    to_date = "04/05/2026"
    
    data = fetch_mcx_bhavcopy(commodity, expiry, from_date, to_date)
    
    if data:
        # STEP 7: OUTPUT FORMAT
        print(f"{'Date':<15} | {'Open':<10} | {'High':<10} | {'Low':<10} | {'Close':<10} | {'Volume':<10} | {'OI':<10}")
        print("-" * 90)
        for row in data:
            print(f"{row['Date']:<15} | {row['Open']:<10} | {row['High']:<10} | {row['Low']:<10} | {row['Close']:<10} | {row['Volume']:<10} | {row['OI']:<10}")
            
if __name__ == "__main__":
    main()
