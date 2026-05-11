"""gg
MCX Actions Fetcher
===================
Stable Selenium fetcher for MCX bhavcopy that works both locally and in
GitHub Actions headless Chrome.

Supported commodities:
    GOLD, GOLDM, SILVER, SILVERM, SILVERMIC, NATURALGAS, NATGASMINI

Key behavior:
    - Uses only the visible MCX UI flow
    - Waits for every element explicitly
    - Selects nearest valid expiry
    - Rolls over to next expiry when current expiry is within 10 trading days
    - Fetches from previous saved date + 1 through yesterday
    - Falls back to first day of previous month if no CSV exists
    - Extracts page 1 + page 2 and merges to CSV
    - Saves screenshots + HTML for every major step and on exceptions
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "mcx_ohlc"
DEBUG_ROOT = PROJECT_ROOT / "mcx_bhavcopy" / "data" / "actions_debug"
MCX_URL = "https://www.mcxindia.com/market-data/bhavcopy"

COMMODITIES = [
    ("GOLD", "gold"),
    ("GOLDM", "goldm"),
    ("SILVER", "silver"),
    ("SILVERM", "silverm"),
    ("SILVERMIC", "silvermic"),
    ("NATURALGAS", "naturalgas"),
    ("NATGASMINI", "naturalgasm"),
]

TRADING_DAY_ROLLOVER_THRESHOLD = 10


@dataclass
class CommodityRun:
    commodity: str
    csv_key: str
    from_date: str
    to_date: str
    expiry: str
    total_rows: int
    added_rows: int
    csv_path: str
    debug_dir: str


def business_days_until(start_day: date, end_day: date) -> int:
    if end_day <= start_day:
        return 0
    current = start_day
    count = 0
    while current < end_day:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def previous_month_start(reference_day: date) -> date:
    first_of_month = reference_day.replace(day=1)
    previous_month_last = first_of_month - timedelta(days=1)
    return previous_month_last.replace(day=1)


def _get_last_date(csv_key: str) -> date | None:
    file_path = DATA_DIR / f"{csv_key}_ohlc.csv"
    if not file_path.exists():
        return None
    try:
        dates = []
        with file_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dates.append(datetime.strptime(row["Date"], "%d %b %Y").date())
                except Exception:
                    pass
        return max(dates) if dates else None
    except Exception:
        return None


def _merge_and_save(csv_key: str, new_rows: list[dict]) -> int:
    if not new_rows:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_path = DATA_DIR / f"{csv_key}_ohlc.csv"
    existing: dict[str, dict] = {}
    if file_path.exists():
        with file_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing[row["Date"]] = row

    added = 0
    for row in new_rows:
        if row["Date"] not in existing:
            added += 1
        existing[row["Date"]] = row

    sorted_rows = sorted(
        existing.values(),
        key=lambda x: datetime.strptime(x["Date"], "%d %b %Y"),
        reverse=True,
    )

    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["Date", "Open", "High", "Low", "Close", "Volume", "OI"],
        )
        writer.writeheader()
        writer.writerows(sorted_rows)
    logger.info(
        f"{csv_key}_ohlc.csv saved - {len(new_rows)} rows "
        f"({added} new, {len(sorted_rows)} total)"
    )
    return added


def _build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
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


def _click_when_ready(driver: webdriver.Chrome, wait: WebDriverWait, locator):
    elem = wait.until(EC.visibility_of_element_located(locator))
    wait.until(EC.element_to_be_clickable(locator))
    try:
        elem.click()
    except Exception:
        driver.execute_script("arguments[0].click();", elem)
    return elem


def _open_page(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    last_exc = None
    for attempt in range(1, 3):
        try:
            logger.info(f"Opening MCX page (attempt {attempt})")
            driver.get(MCX_URL)
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".maToggle.trade1.bhavcopytopsec")))
            wait.until(EC.visibility_of_element_located((By.ID, "ddlInstrumentName")))
            _save_debug(driver, debug_dir, "loaded")
            return
        except Exception as exc:
            last_exc = exc
            _save_debug(driver, debug_dir, f"loaded_error_attempt_{attempt}")
            if attempt == 2:
                raise
    raise last_exc


def _switch_to_commodity_wise(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    logger.info("Switching to Commodity Wise")
    toggle = _click_when_ready(driver, wait, (By.CSS_SELECTOR, ".maToggle.trade1.bhavcopytopsec"))
    time.sleep(1)
    commodity_display = driver.execute_script(
        "return getComputedStyle(document.getElementById('commoditywise')).display;"
    )
    if commodity_display != "block":
        driver.execute_script("arguments[0].click();", toggle)
        time.sleep(1)
        commodity_display = driver.execute_script(
            "return getComputedStyle(document.getElementById('commoditywise')).display;"
        )
    if commodity_display != "block":
        raise RuntimeError("Failed to switch to Commodity Wise mode")
    _save_debug(driver, debug_dir, "commodity_mode")


def _select_instrument(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    logger.info("Selecting Instrument = FUTCOM")
    dropdown = wait.until(EC.element_to_be_clickable((By.ID, "ddlInstrument")))
    Select(dropdown).select_by_value("FUTCOM")
    wait.until(lambda d: d.find_element(By.ID, "ddlInstrument").get_attribute("value") == "FUTCOM")
    _save_debug(driver, debug_dir, "instrument_selected")


def _select_commodity(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    debug_dir: Path,
    commodity: str,
) -> None:
    logger.info(f"Selecting commodity = {commodity}")
    _click_when_ready(driver, wait, (By.ID, "ddlSymbols_Arrow"))
    item = wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, f"//div[@id='ddlSymbols_DropDown']//li[normalize-space()='{commodity}']")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", item)
    try:
        item.click()
    except Exception:
        driver.execute_script("arguments[0].click();", item)
    wait.until(lambda d: d.find_element(By.ID, "ddlSymbols_Input").get_attribute("value").strip() == commodity)
    _save_debug(driver, debug_dir, f"{commodity.lower()}_selected")


def _get_active_expiries(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    debug_dir: Path,
) -> list[tuple[date, str, str]]:
    logger.info("Extracting valid expiries")
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

    valid.sort(key=lambda x: x[0])
    return valid[:2]  # Return top 2 nearest expiries


def _set_one_date(driver: webdriver.Chrome, field_id: str, hidden_id: str, target: date) -> None:
    ui_value = target.strftime("%d/%m/%Y")
    hidden_value = target.strftime("%Y%m%d")
    field = driver.find_element(By.ID, field_id)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
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


def _compute_from_date(csv_key: str, to_day: date, force_days: int) -> date | None:
    if force_days > 0:
        return to_day - timedelta(days=force_days - 1)
    last_dt = _get_last_date(csv_key)
    if last_dt:
        candidate = last_dt + timedelta(days=1)
        if candidate > to_day:
            return None
        return candidate
    return previous_month_start(to_day)


def _set_date_range(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    debug_dir: Path,
    from_day: date,
    to_day: date,
) -> tuple[str, str]:
    if from_day > to_day:
        raise RuntimeError("Invalid date range")
    logger.info(f"Setting date range: {from_day:%d/%m/%Y} -> {to_day:%d/%m/%Y}")
    wait.until(EC.visibility_of_element_located((By.ID, "txtFromDate")))
    wait.until(EC.visibility_of_element_located((By.ID, "txtToDate")))
    _set_one_date(driver, "txtFromDate", "hdnFromDate", from_day)
    _set_one_date(driver, "txtToDate", "hdnToDate", to_day)

    from_txt = driver.find_element(By.ID, "txtFromDate").get_attribute("value")
    to_txt = driver.find_element(By.ID, "txtToDate").get_attribute("value")
    from_hidden = driver.find_element(By.ID, "hdnFromDate").get_attribute("value")
    to_hidden = driver.find_element(By.ID, "hdnToDate").get_attribute("value")
    if from_txt != from_day.strftime("%d/%m/%Y") or from_hidden != from_day.strftime("%Y%m%d"):
        raise RuntimeError("Left date was not set correctly")
    if to_txt != to_day.strftime("%d/%m/%Y") or to_hidden != to_day.strftime("%Y%m%d"):
        raise RuntimeError("Right date was not set correctly")
    _save_debug(driver, debug_dir, "date_selected")
    return from_txt, to_txt


def _load_table(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> None:
    logger.info("Loading table")
    _click_when_ready(driver, wait, (By.ID, "btnShowCommoditywise"))
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")))
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")) > 0)
    time.sleep(2)
    _save_debug(driver, debug_dir, "table_loaded")


def _extract_current_page_rows(driver: webdriver.Chrome) -> list[dict]:
    rows: list[dict] = []
    trs = driver.find_elements(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr")
    for tr in trs:
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) < 15:
            continue
        rows.append(
            {
                "Date": tds[0].text.strip(),
                "Open": tds[6].text.strip(),
                "High": tds[7].text.strip(),
                "Low": tds[8].text.strip(),
                "Close": tds[9].text.strip(),
                "Volume": tds[11].text.strip(),
                "OI": tds[14].text.strip(),
                "_commodity": tds[2].text.strip(),
                "_instrument": tds[1].text.strip(),
                "_expiry": tds[3].text.strip(),
            }
        )
    return rows


def _go_to_page_2(driver: webdriver.Chrome, wait: WebDriverWait, debug_dir: Path) -> bool:
    pager_select_elem = driver.find_elements(By.ID, "ddlPagerBCCW")
    if not pager_select_elem:
        return False
    pager_select = Select(pager_select_elem[0])
    values = [opt.get_attribute("value") for opt in pager_select.options]
    if "2" not in values:
        return False
    logger.info("Extracting page 2")
    pager_select.select_by_value("2")
    driver.execute_script("if (typeof doPagingBCCW === 'function') { doPagingBCCW(); }")
    wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "#tblBhavCopyCommoditywise tbody tr td").text.strip() != "Data not available.")
    time.sleep(2)
    _save_debug(driver, debug_dir, "page2")
    return True


def _verify_rows(rows: list[dict], commodity: str, expiry: str) -> None:
    if not rows:
        raise RuntimeError("No data rows extracted")
    bad_rows = [row for row in rows if row["_commodity"] != commodity]
    if bad_rows:
        raise RuntimeError(f"Commodity verification failed for {commodity}")
    bad_expiry = [row for row in rows if row["_expiry"] != expiry]
    if bad_expiry:
        raise RuntimeError(f"Expiry verification failed for {commodity}")


def _strip_internal_fields(rows: list[dict]) -> list[dict]:
    cleaned = []
    for row in rows:
        cleaned.append(
            {
                "Date": row["Date"],
                "Open": row["Open"],
                "High": row["High"],
                "Low": row["Low"],
                "Close": row["Close"],
                "Volume": row["Volume"],
                "OI": row["OI"],
            }
        )
    return cleaned


def _most_recent_trading_day() -> date:
    """Return the most recent completed weekday (Mon-Fri), skipping weekends.
    
    Fixes the Monday problem: when Action runs on Monday, yesterday is Sunday 
    (no data). We need Friday's data instead.
    """
    candidate = date.today() - timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)
    return candidate


def _fetch_one(
    driver: webdriver.Chrome,
    commodity: str,
    csv_key: str,
    force_days: int,
) -> list[CommodityRun]:
    to_day = _most_recent_trading_day()  # FIX: Skip weekends — Mon→Fri, normal days→yesterday
    
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = DEBUG_ROOT / f"{csv_key}_{stamp}"
    wait = WebDriverWait(driver, 30)

    _open_page(driver, wait, debug_dir)
    _switch_to_commodity_wise(driver, wait, debug_dir)
    _select_instrument(driver, wait, debug_dir)
    _select_commodity(driver, wait, debug_dir, commodity)
    
    expiries = _get_active_expiries(driver, wait, debug_dir)
    runs = []
    
    for exp_date, exp_val, exp_txt in expiries:
        exp_str = exp_date.strftime("%d%b%Y").lower()
        specific_csv_key = f"{csv_key}_{exp_str}"
        
        from_day = _compute_from_date(specific_csv_key, to_day, force_days)
        if from_day is None:
            logger.info(f"{commodity} ({exp_txt}): Already up to date - skipping")
            continue
            
        logger.info(f"Fetching {commodity} for expiry {exp_txt}")
        
        # Select this expiry
        expiry_select = Select(wait.until(EC.visibility_of_element_located((By.ID, "ddlExpiry"))))
        expiry_select.select_by_value(exp_val)
        wait.until(lambda d: d.find_element(By.ID, "ddlExpiry").get_attribute("value") == exp_val)
        
        from_txt, to_txt = _set_date_range(driver, wait, debug_dir, from_day, to_day)
        _load_table(driver, wait, debug_dir)

        rows = _extract_current_page_rows(driver)
        if _go_to_page_2(driver, wait, debug_dir):
            rows.extend(_extract_current_page_rows(driver))
        
        try:
            _verify_rows(rows, commodity, exp_txt)
        except Exception as e:
            logger.warning(f"Verification failed for {commodity} ({exp_txt}): {e}")
            continue

        cleaned_rows = _strip_internal_fields(rows)
        added = _merge_and_save(specific_csv_key, cleaned_rows)
        csv_path = str(DATA_DIR / f"{specific_csv_key}_ohlc.csv")

        runs.append(CommodityRun(
            commodity=commodity,
            csv_key=specific_csv_key,
            from_date=from_txt,
            to_date=to_txt,
            expiry=exp_txt,
            total_rows=len(cleaned_rows),
            added_rows=added,
            csv_path=csv_path,
            debug_dir=str(debug_dir),
        ))
        
    return runs


def run_fetch(force_days: int = 0, only_commodity: str | None = None) -> dict[str, str]:
    summary: dict[str, str] = {}
    targets = [
        item for item in COMMODITIES
        if only_commodity is None or item[0] == only_commodity
    ]
    if only_commodity and not targets:
        raise ValueError(f"Unknown commodity filter: {only_commodity}")

    driver = _build_driver()
    try:
        for commodity, csv_key in targets:
            try:
                logger.info(f"{commodity}: Starting fetch")
                runs = _fetch_one(driver, commodity, csv_key, force_days)
                if not runs:
                    summary[csv_key] = "SKIPPED"
                    continue
                
                for run in runs:
                    summary[run.csv_key] = (
                        f"OK ({run.total_rows} rows, {run.added_rows} new, "
                        f"expiry={run.expiry}, range={run.from_date}->{run.to_date})"
                    )
                logger.info(
                    f"{commodity}: OK | rows={run.total_rows} | added={run.added_rows} | "
                    f"expiry={run.expiry} | range={run.from_date}->{run.to_date}"
                )
            except Exception as exc:
                logger.exception(f"{commodity}: fetch failed")
                summary[csv_key] = f"FAILED ({exc})"
    finally:
        driver.quit()

    logger.info(f"Done: {summary}")
    return summary


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    only_commodity = sys.argv[2].upper() if len(sys.argv) > 2 else None
    if only_commodity:
        logger.info(f"MCX fetch - {only_commodity} - mode days={days}")
    else:
        logger.info(f"MCX fetch - mode days={days}")
    result = run_fetch(force_days=days, only_commodity=only_commodity)
    for key, value in result.items():
        print(f"  {key:15} -> {value}")
    if result and all("FAILED" in value for value in result.values()):
        sys.exit(1)
