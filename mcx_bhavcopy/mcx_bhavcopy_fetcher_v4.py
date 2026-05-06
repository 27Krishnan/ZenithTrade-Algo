from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import json
import csv
import os
from datetime import date, datetime

# Configuration
COMMODITIES = ["GOLD", "GOLDM", "SILVER", "SILVERM", "SILVERMIC", "NATURALGAS", "NATGASMINI"]
INSTRUMENT = "FUTCOM"
FROM_DATE = "01/01/2026"
TO_DATE = date.today().strftime("%d/%m/%Y")
OUTPUT_DIR = r"c:\Users\Admin\OneDrive\Swap Data\Papertrading\mcx_bhavcopy\data"

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

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

def fetch_commodity_data(driver, commodity):
    print(f"Fetching data for {commodity}...")
    try:
        # Navigate to page if not already there
        if driver.current_url != "https://www.mcxindia.com/market-data/bhavcopy":
            driver.get("https://www.mcxindia.com/market-data/bhavcopy")
            time.sleep(5)
            
        # Select 'Commodity Wise' (toggle)
        # Based on subagent, the 'Commodity Wise' is a label/radio that changes the view
        # We can just check if ddlSymbols is present.
        try:
            commodity_wise_toggle = driver.find_element(By.XPATH, "//label[contains(text(), 'Commodity Wise')]")
            commodity_wise_toggle.click()
            time.sleep(2)
        except:
            pass

        # 1. Select Instrument
        instrument_select = driver.find_element(By.ID, "ddlInstrumentName")
        for option in instrument_select.find_elements(By.TAG_NAME, "option"):
            if option.get_attribute("value") == INSTRUMENT:
                option.click()
                break
        time.sleep(3) # Wait for postback to populate symbols

        # 2. Select Commodity (RadComboBox)
        # We use Javascript to interact with Telerik controls
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
        time.sleep(3) # Wait for postback to populate expiry

        # 3. Select Expiry (we'll just take the first non-header option)
        # For historical data from Jan to now, we might need multiple expiries if we want a continuous series,
        # but MCX Bhavcopy usually shows the full history of the commodity if you select an expiry?
        # Actually, Bhavcopy is per contract.
        # But the user said "எக்ஸ்பிரி டேட்டு. இப்போதிக்கு நம்மளுக்கு எக்ஸ்பிரி வந்து நீவ் வந்து நீ ஜூனே வச்சுக்கோ. ஓகேவா? ஜூன்."
        # So I'll try to select the June expiry for each.
        
        # Let's see what expiries are available
        expiry_select = driver.find_element(By.ID, "ddlExpiry")
        target_expiry = None
        for option in expiry_select.find_elements(By.TAG_NAME, "option"):
            text = option.text.upper()
            if "JUN" in text and "2026" in text:
                target_expiry = option
                break
        
        if not target_expiry:
            # Fallback to the first available after header
            options = expiry_select.find_elements(By.TAG_NAME, "option")
            if len(options) > 1:
                target_expiry = options[1]
        
        if target_expiry:
            print(f"Selected Expiry: {target_expiry.text}")
            target_expiry.click()
            time.sleep(2)

        # 4. Set Dates
        from_date_input = driver.find_element(By.ID, "txtFromDate")
        driver.execute_script(f"arguments[0].value = '{FROM_DATE}';", from_date_input)
        
        to_date_input = driver.find_element(By.ID, "txtToDate")
        driver.execute_script(f"arguments[0].value = '{TO_DATE}';", to_date_input)
        
        # 5. Click Show
        show_btn = driver.find_element(By.ID, "btnShowCommoditywise")
        show_btn.click()
        
        # 6. Wait for vBC to update (it usually takes a few seconds)
        time.sleep(5)
        
        # 7. Extract vBC
        data_json = driver.execute_script("return JSON.stringify(vBC);")
        if data_json and data_json != "null":
            data = json.loads(data_json)
            return data
        else:
            print(f"No data found for {commodity}")
            return None

    except Exception as e:
        print(f"Error fetching {commodity}: {e}")
        return None

def save_data(commodity, data):
    if not data:
        return
    
    file_path = os.path.join(OUTPUT_DIR, f"{commodity.lower()}_history.csv")
    
    # Extract headers from the first record
    if not data:
        return
    
    # Map fields to match user requirement (OHLC)
    # MCX fields: Date, Instrument, Commodity, Expiry, Open, High, Low, Close, PCP, Volume, Volume000, Value, OI
    # We want to be sure about the field names.
    
    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    
    print(f"Saved {len(data)} rows for {commodity} to {file_path}")

def main():
    driver = setup_driver()
    try:
        for commodity in COMMODITIES:
            data = fetch_commodity_data(driver, commodity)
            if data:
                save_data(commodity, data)
            time.sleep(2)
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
