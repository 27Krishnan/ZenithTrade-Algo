"""
Stable GOLDM-only Selenium fetcher for MCX Bhavcopy.

Flow:
1. Open MCX Bhavcopy page
2. Switch to Commodity Wise
3. Select FUTCOM
4. Select GOLDM from visible commodity dropdown
5. Select nearest valid future expiry
6. Set left date = first day of previous month, right date = yesterday
7. Load table, verify GOLDM rows, extract page 1 + page 2
8. Save CSV and debug artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


MCX_URL = "https://www.mcxindia.com/market-data/bhavcopy"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "goldm_runs"


@dataclass
class FetchResult:
    total_rows: int
    selected_expiry: str
    from_date: str
    to_date: str
    csv_path: str
    debug_dir: str


def log(step: str) -> None:
    print(f"[INFO] {step}")


def build_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def save_debug(driver: webdriver.Chrome, debug_dir: Path, stem: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(debug_dir / f"{stem}.png"))
    (debug_dir / f"{stem}.html").write_text(driver.page_source, encoding="utf-8")


def click_when_ready(driver: webdriver.Chrome, wait: WebDriverWait, locator, label: str):
    elem = wait.until(EC.visibility_of_element_located(locator))
    wait.until(EC.element_to_be_clickable(locator))
    try:
        elem.click()
    except Exception:
        driver.execute_script("arguments[0].click();", elem)
    return elem


def open_page(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    last_exc = None
    for attempt in range(1, 3):
        try:
            log(f"Opening MCX page (attempt {attempt})")
            driver.get(MCX_URL)
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".maToggle.trade1.bhavcopytopsec")))
            wait.until(EC.visibility_of_element_located((By.ID, "ddlInstrumentName")))
            save_debug(driver, debug_dir, "loaded")
            return
        except Exception as exc:
            last_exc = exc
            save_debug(driver, debug_dir, f"loaded_error_attempt_{attempt}")
            if attempt == 2:
                raise
    raise last_exc


def switch_to_commodity_wise(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    log("Switching to Commodity Wise")
    toggle = click_when_ready(
        driver,
        wait,
        (By.CSS_SELECTOR, ".maToggle.trade1.bhavcopytopsec"),
        "Commodity Wise toggle",
    )
    time.sleep(1)
    commodity_display = driver.execute_script(
        "return getComputedStyle(document.getElementById('commoditywise')).display;"
    )
    datewise_display = driver.execute_script(
        "return getComputedStyle(document.getElementById('datewise')).display;"
    )
    if commodity_display != "block":
        driver.execute_script("arguments[0].click();", toggle)
        time.sleep(1)
        commodity_display = driver.execute_script(
            "return getComputedStyle(document.getElementById('commoditywise')).display;"
        )
    if commodity_display != "block" or datewise_display != "none":
        raise RuntimeError("Failed to switch to Commodity Wise mode")
    save_debug(driver, debug_dir, "commodity_mode")


def select_instrument(wait: WebDriverWait, debug_dir: Path, driver: webdriver.Chrome) -> None:
    log("Selecting Instrument = FUTCOM")
    dropdown = wait.until(EC.element_to_be_clickable((By.ID, "ddlInstrument")))
    Select(dropdown).select_by_value("FUTCOM")
    wait.until(lambda d: d.find_element(By.ID, "ddlInstrument").get_attribute("value") == "FUTCOM")
    save_debug(driver, debug_dir, "instrument_selected")


def select_commodity(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    log("Selecting commodity = GOLDM")
    click_when_ready(driver, wait, (By.ID, "ddlSymbols_Arrow"), "Commodity arrow")
    item = wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//div[@id='ddlSymbols_DropDown']//li[normalize-space()='GOLDM']")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", item)
    try:
        item.click()
    except Exception:
        driver.execute_script("arguments[0].click();", item)
    wait.until(lambda d: d.find_element(By.ID, "ddlSymbols_Input").get_attribute("value").strip() == "GOLDM")
    save_debug(driver, debug_dir, "goldm_selected")


def choose_nearest_expiry(wait: WebDriverWait, driver: webdriver.Chrome, debug_dir: Path) -> str:
    log("Selecting nearest future expiry")
    expiry_select = Select(wait.until(EC.visibility_of_element_located((By.ID, "ddlExpiry"))))
    today = date.today()
    valid: list[tuple[date, str, str]] = []
    for option in expiry_select.options:
        txt = option.text.strip()
        if not txt or txt.lower().startswith("select"):
            continue
        try:
            exp = datetime.strptime(txt, "%d%b%Y").date()
        except ValueError:
            continue
        if exp >= today:
            valid.append((exp, option.get_attribute("value"), txt))
    if not valid:
        raise RuntimeError("No valid future expiry found in dropdown")
    nearest = min(valid, key=lambda x: x[0])
    expiry_select.select_by_value(nearest[1])
    wait.until(lambda d: d.find_element(By.ID, "ddlExpiry").get_attribute("value") == nearest[1])
    save_debug(driver, debug_dir, "expiry_selected")
    return nearest[2]


def previous_month_start(reference_day: date) -> date:
    first_of_month = reference_day.replace(day=1)
    previous_month_last = first_of_month - timedelta(days=1)
    return previous_month_last.replace(day=1)


def _set_one_date(driver: webdriver.Chrome, field_id: str, hidden_id: str, target: date) -> None:
    ui_value = target.strftime("%d/%m/%Y")
    hidden_value = target.strftime("%Y%m%d")
    field = driver.find_element(By.ID, field_id)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)

    # First try the site's own datepicker API.
    driver.execute_script(
        """
        const fieldId = arguments[0];
        const hiddenId = arguments[1];
        const year = arguments[2];
        const monthZero = arguments[3];
        const day = arguments[4];
        const hiddenValue = arguments[5];
        try {
            $('#' + fieldId).datepick('setDate', new Date(year, monthZero, day));
        } catch (e) {}
        document.getElementById(hiddenId).value = hiddenValue;
        """,
        field_id,
        hidden_id,
        target.year,
        target.month - 1,
        target.day,
        hidden_value,
    )

    actual_txt = driver.find_element(By.ID, field_id).get_attribute("value")
    actual_hidden = driver.find_element(By.ID, hidden_id).get_attribute("value")
    if actual_txt != ui_value or actual_hidden != hidden_value:
        driver.execute_script(
            """
            document.getElementById(arguments[0]).value = arguments[1];
            document.getElementById(arguments[2]).value = arguments[3];
            """,
            field_id,
            ui_value,
            hidden_id,
            hidden_value,
        )


def set_date_range(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> tuple[str, str]:
    yesterday = date.today() - timedelta(days=1)
    from_day = previous_month_start(yesterday)
    if from_day >= yesterday:
        raise RuntimeError("Invalid date range: from date must be before yesterday")

    log(f"Setting date range: {from_day:%d/%m/%Y} -> {yesterday:%d/%m/%Y}")
    wait.until(EC.visibility_of_element_located((By.ID, "txtFromDate")))
    wait.until(EC.visibility_of_element_located((By.ID, "txtToDate")))

    _set_one_date(driver, "txtFromDate", "hdnFromDate", from_day)
    _set_one_date(driver, "txtToDate", "hdnToDate", yesterday)

    from_txt = driver.find_element(By.ID, "txtFromDate").get_attribute("value")
    to_txt = driver.find_element(By.ID, "txtToDate").get_attribute("value")
    from_hidden = driver.find_element(By.ID, "hdnFromDate").get_attribute("value")
    to_hidden = driver.find_element(By.ID, "hdnToDate").get_attribute("value")

    if from_txt != from_day.strftime("%d/%m/%Y") or from_hidden != from_day.strftime("%Y%m%d"):
        raise RuntimeError("Left date picker was not set correctly")
    if to_txt != yesterday.strftime("%d/%m/%Y") or to_hidden != yesterday.strftime("%Y%m%d"):
        raise RuntimeError("Right date picker was not set correctly")

    save_debug(driver, debug_dir, "date_selected")
    return from_txt, to_txt


def load_table(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    log("Loading table")
    click_when_ready(driver, wait, (By.ID, "btnShowCommoditywise"), "Show button")
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")))
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")) > 0)
    time.sleep(2)
    save_debug(driver, debug_dir, "table_loaded")


def extract_current_page_rows(driver: webdriver.Chrome) -> list[dict]:
    rows: list[dict] = []
    trs = driver.find_elements(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")
    for tr in trs:
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) < 15:
            continue
        row = {
            "Date": tds[0].text.strip(),
            "Instrument": tds[1].text.strip(),
            "Commodity": tds[2].text.strip(),
            "Expiry": tds[3].text.strip(),
            "Open": tds[6].text.strip(),
            "High": tds[7].text.strip(),
            "Low": tds[8].text.strip(),
            "Close": tds[9].text.strip(),
            "PrevClose": tds[10].text.strip(),
            "Volume": tds[11].text.strip(),
            "OI": tds[14].text.strip(),
        }
        rows.append(row)
    return rows


def go_to_page_2(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> bool:
    pager_select_elem = driver.find_elements(By.ID, "ddlPagerBCCW")
    if not pager_select_elem:
        return False
    pager_select = Select(pager_select_elem[0])
    values = [opt.get_attribute("value") for opt in pager_select.options]
    if "2" not in values:
        return False

    log("Extracting page 2")
    pager_select.select_by_value("2")
    driver.execute_script("if (typeof doPagingBCCW === 'function') { doPagingBCCW(); }")
    wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr td").text.strip() != "Data not available.")
    time.sleep(2)
    save_debug(driver, debug_dir, "page2")
    return True


def verify_rows(rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError("No data rows extracted")
    bad_rows = [row for row in rows if row["Commodity"].strip() != "GOLDM"]
    if bad_rows:
        raise RuntimeError(f"Commodity verification failed. Non-GOLDM rows found: {len(bad_rows)}")


def save_csv(rows: list[dict], run_day: date, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"goldm_bhavcopy_{run_day:%Y%m%d}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Date",
                "Instrument",
                "Commodity",
                "Expiry",
                "Open",
                "High",
                "Low",
                "Close",
                "PrevClose",
                "Volume",
                "OI",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def fetch_goldm(headless: bool = True) -> FetchResult:
    run_day = date.today()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = OUTPUT_DIR / f"debug_{stamp}"
    driver = build_driver(headless=headless)
    wait = WebDriverWait(driver, 30)

    try:
        open_page(driver, wait, debug_dir)
        switch_to_commodity_wise(driver, wait, debug_dir)
        select_instrument(wait, debug_dir, driver)
        select_commodity(driver, wait, debug_dir)
        selected_expiry = choose_nearest_expiry(wait, driver, debug_dir)
        from_date, to_date = set_date_range(driver, wait, debug_dir)
        load_table(driver, wait, debug_dir)

        rows = extract_current_page_rows(driver)
        page2_rows: list[dict] = []
        if go_to_page_2(driver, wait, debug_dir):
            page2_rows = extract_current_page_rows(driver)

        all_rows = rows + page2_rows
        verify_rows(all_rows)
        csv_path = save_csv(all_rows, run_day, debug_dir)

        return FetchResult(
            total_rows=len(all_rows),
            selected_expiry=selected_expiry,
            from_date=from_date,
            to_date=to_date,
            csv_path=str(csv_path),
            debug_dir=str(debug_dir),
        )
    except Exception:
        save_debug(driver, debug_dir, "exception")
        raise
    finally:
        driver.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch GOLDM MCX bhavcopy via Selenium")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    args = parser.parse_args()

    result = fetch_goldm(headless=args.headless)
    print(json.dumps(result.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
