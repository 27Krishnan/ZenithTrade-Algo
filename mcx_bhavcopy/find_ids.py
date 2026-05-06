from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
import time
import json
import csv
from datetime import date

def get_mcx_ids():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    try:
        driver.get("https://www.mcxindia.com/market-data/bhavcopy")
        time.sleep(10)
        
        print("--- SELECT ELEMENTS ---")
        for s in driver.find_elements(By.TAG_NAME, "select"):
            print(f"ID: {s.get_attribute('id')} | Name: {s.get_attribute('name')}")
            
        print("\n--- INPUT ELEMENTS ---")
        for i in driver.find_elements(By.TAG_NAME, "input"):
            print(f"ID: {i.get_attribute('id')} | Name: {i.get_attribute('name')} | Type: {i.get_attribute('type')}")
            
        print("\n--- BUTTON ELEMENTS ---")
        for b in driver.find_elements(By.TAG_NAME, "button"):
            print(f"ID: {b.get_attribute('id')} | Text: {b.text}")
            
        # Also check for links/icons that look like Show
        print("\n--- LINKS ---")
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if "Show" in a.text or "btnShow" in a.get_attribute("id") or "btnShow" in a.get_attribute("class"):
                print(f"ID: {a.get_attribute('id')} | Text: {a.text}")

    finally:
        driver.quit()

if __name__ == "__main__":
    get_mcx_ids()
