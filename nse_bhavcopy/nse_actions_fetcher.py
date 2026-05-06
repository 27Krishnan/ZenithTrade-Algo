import os
import csv
import sys
import time
from datetime import datetime, date
from pathlib import Path

from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_DIR = Path(__file__).parent.parent.absolute()
DATA_DIR = BASE_DIR / "data" / "nse_ohlc"
DEBUG_ROOT = Path(__file__).parent / "data" / "nifty_runs"

DATA_DIR.mkdir(parents=True, exist_ok=True)

def _build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    chrome_path = "/usr/bin/google-chrome-stable"
    if os.path.exists(chrome_path):
        opts.binary_location = chrome_path

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

def _save_debug(driver: webdriver.Chrome, debug_dir: Path, stem: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(debug_dir / f"{stem}.png"))
    (debug_dir / f"{stem}.html").write_text(driver.page_source, encoding="utf-8")

def _wait_for_table_load(driver, wait):
    # Wait until the loader overlay goes away if it exists
    try:
        wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loader")))
        time.sleep(1) # Extra buffer
    except Exception:
        pass

def fetch_nifty_data():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = DEBUG_ROOT / f"debug_{stamp}"
    
    driver = _build_driver()
    wait = WebDriverWait(driver, 30)
    summary = {}
    
    try:
        logger.info("Opening NSE page...")
        driver.get("https://www.nseindia.com/get-quote/derivatives/NIFTY/NIFTY%2050")
        
        # Click Historical Data Tab
        logger.info("Clicking Historical Data tab")
        tab = wait.until(EC.element_to_be_clickable((By.ID, "derivatives-tabs-tab-historical-data")))
        driver.execute_script("arguments[0].click();", tab)
        _wait_for_table_load(driver, wait)
        
        # Select Data Type (Index Futures)
        logger.info("Selecting Index Futures")
        data_type_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[1]'))))
        data_type_select.select_by_visible_text("Index Futures")
        _wait_for_table_load(driver, wait)
        
        # Select Year (2026)
        logger.info("Selecting Year 2026")
        year_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[2]'))))
        year_select.select_by_visible_text("2026")
        _wait_for_table_load(driver, wait)
        
        # Get Expiries
        logger.info("Extracting valid expiries")
        expiry_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[3]'))))
        
        today = date.today()
        from collections import defaultdict
        month_expiries = defaultdict(list)
        
        for option in expiry_select.options:
            txt = option.text.strip()
            if not txt or txt.lower() == "select":
                continue
            try:
                # Format is usually '28-May-2026'
                exp = datetime.strptime(txt, "%d-%b-%Y").date()
            except ValueError:
                continue
            
            if exp >= today:
                month_expiries[(exp.year, exp.month)].append((exp, option.get_attribute("value"), txt))
                
        valid = []
        for (y, m), exp_list in month_expiries.items():
            exp_list.sort(key=lambda x: x[0])
            valid.append(exp_list[-1]) # Pick the LAST expiry of the month
            
        if not valid:
            raise RuntimeError("No valid future expiry found in dropdown")
            
        valid.sort(key=lambda x: x[0])
        top_expiries = valid[:2] # Top 2 monthly expiries
        
        for exp_date, exp_val, exp_txt in top_expiries:
            exp_str = exp_date.strftime("%d%b%Y").lower()
            csv_key = f"nifty_{exp_str}"
            logger.info(f"Fetching NIFTY for expiry {exp_txt}")
            
            # REFRESH PAGE STATE FOR EACH EXPIRY
            driver.refresh()
            _wait_for_table_load(driver, wait)
            
            tab = wait.until(EC.element_to_be_clickable((By.ID, "derivatives-tabs-tab-historical-data")))
            driver.execute_script("arguments[0].click();", tab)
            _wait_for_table_load(driver, wait)
            
            data_type_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[1]'))))
            data_type_select.select_by_visible_text("Index Futures")
            _wait_for_table_load(driver, wait)
            
            year_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[2]'))))
            year_select.select_by_visible_text("2026")
            _wait_for_table_load(driver, wait)
            
            # Select Expiry
            expiry_select = Select(wait.until(EC.presence_of_element_located((By.XPATH, '(//select[@class="custom-select width-200"])[3]'))))
            expiry_select.select_by_visible_text(exp_txt)
            _wait_for_table_load(driver, wait)
            
            # Click 1Y button to expand date range
            logger.info("Clicking 1Y button to get full historical data")
            one_year_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[text()="1Y"]')))
            driver.execute_script("arguments[0].click();", one_year_btn)
            _wait_for_table_load(driver, wait)
            time.sleep(2) # Give table time to render completely
            _save_debug(driver, debug_dir, f"table_loaded_{exp_str}")
            
            # Extract Data
            table = driver.find_element(By.XPATH, '//div[@class="nse-table-responsive"]')
            
            # Dynamically find column indices
            headers = [th.text.strip().lower() for th in table.find_elements(By.XPATH, './/thead/tr/th')]
            try:
                idx_date = headers.index('date')
                idx_open = headers.index('open price')
                idx_high = headers.index('high price')
                idx_low = headers.index('low price')
                idx_close = headers.index('close price')
            except ValueError:
                # Fallback indices if header names differ slightly
                idx_date, idx_open, idx_high, idx_low, idx_close = 0, 4, 5, 6, 7
                
            rows = table.find_elements(By.XPATH, './/tbody/tr')
            
            extracted_data = []
            for row in rows:
                cols = row.find_elements(By.TAG_NAME, 'td')
                if len(cols) > max(idx_date, idx_open, idx_high, idx_low, idx_close):
                    dt_text = cols[idx_date].text.strip()
                    if not dt_text or dt_text.lower() == "no records found":
                        continue
                    open_price = cols[idx_open].text.strip().replace(',', '')
                    high_price = cols[idx_high].text.strip().replace(',', '')
                    low_price = cols[idx_low].text.strip().replace(',', '')
                    close_price = cols[idx_close].text.strip().replace(',', '')
                    
                    extracted_data.append({
                        "Date": dt_text,
                        "Open": open_price,
                        "High": high_price,
                        "Low": low_price,
                        "Close": close_price
                    })
            
            if not extracted_data:
                logger.warning(f"No data extracted for {exp_txt}")
                summary[csv_key] = "FAILED: No data"
                continue
                
            # Sort newest first based on Date
            extracted_data.sort(key=lambda x: datetime.strptime(x["Date"], "%d-%b-%Y"), reverse=True)
            
            # Merge logic - similar to MCX, read existing, append new, rewrite
            csv_path = DATA_DIR / f"{csv_key}_ohlc.csv"
            existing = {}
            if csv_path.exists():
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        existing[r["Date"]] = r
                        
            added = 0
            for r in extracted_data:
                if r["Date"] not in existing:
                    added += 1
                existing[r["Date"]] = r
                
            sorted_rows = sorted(
                existing.values(),
                key=lambda x: datetime.strptime(x["Date"], "%d-%b-%Y"),
                reverse=True,
            )
            
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close"])
                writer.writeheader()
                writer.writerows(sorted_rows)
                
            logger.info(f"{csv_key}_ohlc.csv saved - {len(extracted_data)} rows ({added} new, {len(sorted_rows)} total)")
            summary[csv_key] = f"OK ({len(extracted_data)} rows, {added} new, expiry={exp_txt})"
            
    except Exception as e:
        logger.exception("Error during NSE fetch")
        _save_debug(driver, debug_dir, "error")
        summary["error"] = str(e)
    finally:
        driver.quit()
        
    return summary

if __name__ == "__main__":
    logger.info("Starting NSE Historical Fetcher")
    res = fetch_nifty_data()
    logger.info(f"Summary: {res}")
