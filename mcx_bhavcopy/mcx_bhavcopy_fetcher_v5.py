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
from datetime import date

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

def wait_for_ajax(driver):
    wait = WebDriverWait(driver, 10)
    # Wait for the loading overlay to disappear if it exists
    try:
        wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "raDiv")))
    except:
        pass
    time.sleep(1)

def fetch_commodity_data(driver, commodity):
    print(f"\nProcessing {commodity}...")
    try:
        driver.get("https://www.mcxindia.com/market-data/bhavcopy")
        time.sleep(5)
        
        # Switch to 'Commodity Wise'
        # The toggle is often a label for a radio button
        try:
            # Look for the radio button or label
            radio = driver.find_element(By.XPATH, "//input[@value='CommodityWise']")
            driver.execute_script("arguments[0].click();", radio)
            print("Switched to Commodity Wise")
            time.sleep(2)
        except Exception as e:
            print(f"Warning: Could not click Commodity Wise toggle via radio: {e}")
            # Try by label text
            try:
                label = driver.find_element(By.XPATH, "//label[contains(text(), 'Commodity Wise')]")
                label.click()
                time.sleep(2)
            except:
                pass

        # 1. Select Instrument
        wait = WebDriverWait(driver, 10)
        instrument_select = wait.until(EC.presence_of_element_located((By.ID, "ddlInstrumentName")))
        driver.execute_script(f"arguments[0].value = '{INSTRUMENT}';", instrument_select)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'))")
        print(f"Selected Instrument: {INSTRUMENT}")
        time.sleep(3)
        wait_for_ajax(driver)

        # 2. Select Commodity (RadComboBox)
        # Using Telerik API via Javascript
        driver.execute_script(f"""
            var combo = $find("ddlSymbols");
            if (combo) {{
                var item = combo.findItemByText("{commodity}");
                if (item) {{
                    item.select();
                }} else {{
                    combo.set_text("{commodity}");
                    // Trigger postback if needed
                    combo.raise_selectedIndexChanged();
                }}
            }}
        """)
        print(f"Selected Commodity: {commodity}")
        time.sleep(3)
        wait_for_ajax(driver)

        # 3. Select Expiry
        expiry_select = wait.until(EC.presence_of_element_located((By.ID, "ddlExpiry")))
        # We want the June expiry if possible, otherwise the first one
        options = expiry_select.find_elements(By.TAG_NAME, "option")
        selected_text = "None"
        found = False
        for opt in options:
            if "JUN" in opt.text.upper() and "2026" in opt.text:
                driver.execute_script("arguments[0].selected = true;", opt)
                selected_text = opt.text
                found = True
                break
        
        if not found and len(options) > 1:
            driver.execute_script("arguments[0].selected = true;", options[1])
            selected_text = options[1].text
            
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'))", expiry_select)
        print(f"Selected Expiry: {selected_text}")
        time.sleep(2)
        wait_for_ajax(driver)

        # 4. Set Dates
        driver.execute_script(f"document.getElementById('txtFromDate').value = '{FROM_DATE}';")
        driver.execute_script(f"document.getElementById('txtToDate').value = '{TO_DATE}';")
        print(f"Set Date Range: {FROM_DATE} to {TO_DATE}")

        # 5. Click Show
        show_btn = driver.find_element(By.ID, "btnShowCommoditywise")
        driver.execute_script("arguments[0].click();", show_btn)
        print("Clicked Show")
        time.sleep(5)
        wait_for_ajax(driver)

        # 6. Extract vBC
        data_json = driver.execute_script("return JSON.stringify(vBC);")
        if data_json and data_json != "null" and data_json != "[]":
            data = json.loads(data_json)
            print(f"Found {len(data)} records")
            return data
        else:
            print(f"No records found in vBC for {commodity}")
            return None

    except Exception as e:
        print(f"Error processing {commodity}: {e}")
        return None

def save_data(commodity, data):
    if not data:
        return
    
    file_path = os.path.join(OUTPUT_DIR, f"{commodity.lower()}_history.csv")
    
    # Filter for relevant fields if needed, but saving all for now
    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    
    print(f"Successfully saved {commodity} data to {file_path}")

def main():
    driver = setup_driver()
    try:
        for commodity in COMMODITIES:
            data = fetch_commodity_data(driver, commodity)
            if data:
                save_data(commodity, data)
            else:
                print(f"Failed to fetch data for {commodity}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
