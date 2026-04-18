"""Headless web UI scraper adapter for Application URL and GCAS Launcher checks.

Uses Selenium with headless ChromeDriver to perform click-path navigation
on web application URLs, verifying that pages load correctly and expected
elements are present.

All operations are read-only (no form submissions or data modifications).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import WebUISettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("webui_scraper_adapter")


class WebUIScraperAdapter(BaseAdapter):
    """Adapter for headless web UI navigation and validation.

    Performs click-path checks on web applications using Selenium
    with headless Chrome. Validates that:
    - Pages load within timeout
    - Expected elements/text are present
    - No critical errors appear on the page
    """

    def __init__(self, settings: WebUISettings) -> None:
        self._settings = settings
        self._timeout = settings.page_timeout_seconds
        self._allowed_domains = settings.allowed_domains

    @property
    def adapter_name(self) -> str:
        return "WebUIScraper"

    async def health_check(self) -> bool:
        """Check that Selenium/ChromeDriver is available."""
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            driver = webdriver.Chrome(options=options)
            driver.quit()
            return True
        except Exception as exc:
            logger.warning("WebUI scraper health check failed: %s", exc)
            return False

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute a web UI check.

        Parameters:
            url (str): The URL to navigate to.
            check_type (str): "page_load", "click_path", or "element_check".
            click_steps (list): List of click-path steps for click_path check.
                Each step: {"action": "click"|"wait"|"assert_text"|"assert_element",
                            "selector": "css_selector", "value": "text_or_timeout"}
            expected_text (str): Text expected on the page.
            expected_element (str): CSS selector of expected element.
            screenshot (bool): Whether to capture a screenshot (default false).
        """
        url = parameters.get("url", "")
        if not url:
            return AdapterResult(success=False, error="No URL provided")

        # Domain allow-list check
        if self._allowed_domains:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.hostname and not any(
                parsed.hostname.endswith(d) for d in self._allowed_domains
            ):
                return AdapterResult(
                    success=False,
                    error=f"Domain {parsed.hostname} not in allowed domains list",
                )

        check_type = parameters.get("check_type", "page_load")

        try:
            if check_type == "page_load":
                return await self._check_page_load(url, parameters)
            elif check_type == "click_path":
                return await self._execute_click_path(url, parameters)
            elif check_type == "element_check":
                return await self._check_element(url, parameters)
            else:
                return AdapterResult(
                    success=False,
                    error=f"Unknown check_type: {check_type}",
                )
        except Exception as exc:
            metrics.increment("webui.checks_failed")
            logger.error("WebUI check failed for %s: %s", url, exc)
            return AdapterResult(success=False, error=str(exc))

    async def _check_page_load(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Verify a page loads successfully within timeout."""
        return await asyncio.to_thread(
            self._sync_check_page_load, url, parameters
        )

    def _sync_check_page_load(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Synchronous page-load check, run in a thread."""
        expected_text = parameters.get("expected_text", "")

        driver = self._create_driver()
        try:
            start = time.monotonic()
            driver.set_page_load_timeout(self._timeout)
            driver.get(url)
            load_time = time.monotonic() - start

            title = driver.title
            page_source_len = len(driver.page_source)

            # Check for expected text
            text_found = True
            if expected_text:
                text_found = expected_text.lower() in driver.page_source.lower()

            # Check for common error indicators
            error_indicators = _detect_errors(driver.page_source)

            evidence_lines = [
                f"Page Load Check | URL: {url}",
                f"  Title: {title}",
                f"  Load time: {load_time:.2f}s",
                f"  Page size: {page_source_len} bytes",
            ]
            if expected_text:
                evidence_lines.append(
                    f"  Expected text '{expected_text}': {'FOUND' if text_found else 'NOT FOUND'}"
                )
            if error_indicators:
                evidence_lines.append(f"  Error indicators: {', '.join(error_indicators)}")
            else:
                evidence_lines.append("  No error indicators detected")

            success = text_found and not error_indicators

            metrics.increment("webui.page_load_checks")
            return AdapterResult(
                success=success,
                data={
                    "url": url,
                    "title": title,
                    "load_time_seconds": round(load_time, 2),
                    "page_size_bytes": page_source_len,
                    "expected_text_found": text_found,
                    "error_indicators": error_indicators,
                    "status": "healthy" if success else "unhealthy",
                },
                raw_output=json.dumps({
                    "title": title,
                    "load_time": load_time,
                    "errors": error_indicators,
                }),
                evidence_snippet="\n".join(evidence_lines),
            )
        finally:
            driver.quit()

    async def _execute_click_path(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Execute a series of click-path steps on a web page."""
        click_steps: List[Dict[str, str]] = parameters.get("click_steps", [])
        if not click_steps:
            return AdapterResult(
                success=False,
                error="No click_steps provided for click_path check",
            )
        return await asyncio.to_thread(
            self._sync_execute_click_path, url, click_steps
        )

    def _sync_execute_click_path(
        self, url: str, click_steps: List[Dict[str, str]]
    ) -> AdapterResult:
        """Synchronous click-path execution, run in a thread."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._create_driver()
        step_results: List[Dict[str, Any]] = []

        try:
            start = time.monotonic()
            driver.set_page_load_timeout(self._timeout)
            driver.get(url)

            step_results.append({
                "step": "navigate",
                "url": url,
                "status": "success",
                "title": driver.title,
            })

            for i, step in enumerate(click_steps):
                step_action = step.get("action", "")
                selector = step.get("selector", "")
                value = step.get("value", "")
                step_name = step.get("name", f"step-{i+1}")

                try:
                    if step_action == "click":
                        wait = WebDriverWait(driver, self._timeout)
                        element = wait.until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        element.click()
                        step_results.append({
                            "step": step_name,
                            "action": "click",
                            "selector": selector,
                            "status": "success",
                        })

                    elif step_action == "wait":
                        wait_seconds = float(value) if value else 2.0
                        time.sleep(wait_seconds)
                        step_results.append({
                            "step": step_name,
                            "action": "wait",
                            "duration": wait_seconds,
                            "status": "success",
                        })

                    elif step_action == "assert_text":
                        text_present = value.lower() in driver.page_source.lower()
                        step_results.append({
                            "step": step_name,
                            "action": "assert_text",
                            "expected": value,
                            "found": text_present,
                            "status": "success" if text_present else "fail",
                        })

                    elif step_action == "assert_element":
                        wait = WebDriverWait(driver, self._timeout)
                        try:
                            wait.until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                            )
                            step_results.append({
                                "step": step_name,
                                "action": "assert_element",
                                "selector": selector,
                                "status": "success",
                            })
                        except Exception:
                            step_results.append({
                                "step": step_name,
                                "action": "assert_element",
                                "selector": selector,
                                "status": "fail",
                                "error": f"Element not found: {selector}",
                            })

                    else:
                        step_results.append({
                            "step": step_name,
                            "action": step_action,
                            "status": "skip",
                            "error": f"Unknown action: {step_action}",
                        })

                except Exception as exc:
                    step_results.append({
                        "step": step_name,
                        "action": step_action,
                        "status": "fail",
                        "error": str(exc),
                    })

            total_time = time.monotonic() - start
            all_passed = all(s.get("status") == "success" for s in step_results)

            evidence_lines = [f"Click Path Check | URL: {url}"]
            evidence_lines.append(f"  Steps executed: {len(step_results)}")
            evidence_lines.append(f"  Total time: {total_time:.2f}s")
            for sr in step_results:
                status_icon = "OK" if sr["status"] == "success" else "FAIL"
                evidence_lines.append(
                    f"  [{status_icon}] {sr.get('step', '?')}: {sr.get('action', '?')}"
                )
                if sr.get("error"):
                    evidence_lines.append(f"         Error: {sr['error']}")

            metrics.increment("webui.click_path_checks")
            return AdapterResult(
                success=all_passed,
                data={
                    "url": url,
                    "steps_executed": len(step_results),
                    "steps_passed": sum(1 for s in step_results if s["status"] == "success"),
                    "total_time_seconds": round(total_time, 2),
                    "step_results": step_results,
                    "all_passed": all_passed,
                },
                raw_output=json.dumps(step_results),
                evidence_snippet="\n".join(evidence_lines),
            )
        finally:
            driver.quit()

    async def _check_element(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Check for specific element presence on a page."""
        return await asyncio.to_thread(
            self._sync_check_element, url, parameters
        )

    def _sync_check_element(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Synchronous element check, run in a thread."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        expected_element = parameters.get("expected_element", "")
        expected_text = parameters.get("expected_text", "")

        driver = self._create_driver()
        try:
            start = time.monotonic()
            driver.set_page_load_timeout(self._timeout)
            driver.get(url)

            results: Dict[str, Any] = {
                "url": url,
                "title": driver.title,
            }
            evidence_lines = [f"Element Check | URL: {url}"]

            if expected_element:
                try:
                    wait = WebDriverWait(driver, self._timeout)
                    element = wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, expected_element))
                    )
                    results["element_found"] = True
                    results["element_text"] = element.text[:200] if element.text else ""
                    evidence_lines.append(f"  Element '{expected_element}': FOUND")
                except Exception:
                    results["element_found"] = False
                    evidence_lines.append(f"  Element '{expected_element}': NOT FOUND")

            if expected_text:
                text_found = expected_text.lower() in driver.page_source.lower()
                results["text_found"] = text_found
                evidence_lines.append(
                    f"  Text '{expected_text}': {'FOUND' if text_found else 'NOT FOUND'}"
                )

            load_time = time.monotonic() - start
            results["load_time_seconds"] = round(load_time, 2)
            evidence_lines.append(f"  Load time: {load_time:.2f}s")

            success = results.get("element_found", True) and results.get("text_found", True)

            metrics.increment("webui.element_checks")
            return AdapterResult(
                success=success,
                data=results,
                raw_output=json.dumps(results),
                evidence_snippet="\n".join(evidence_lines),
            )
        finally:
            driver.quit()

    # ── Internal helpers ──────────────────────────────────────────────

    def _create_driver(self) -> Any:
        """Create a headless Chrome WebDriver instance."""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        if self._settings.chrome_binary_path:
            options.binary_location = self._settings.chrome_binary_path

        driver = webdriver.Chrome(options=options)
        return driver


def _detect_errors(page_source: str) -> List[str]:
    """Detect common error indicators in page HTML."""
    indicators: List[str] = []
    lower_source = page_source.lower()

    error_patterns = [
        ("500 internal server error", "HTTP 500"),
        ("502 bad gateway", "HTTP 502"),
        ("503 service unavailable", "HTTP 503"),
        ("504 gateway timeout", "HTTP 504"),
        ("application error", "Application Error"),
        ("page not found", "Page Not Found"),
        ("access denied", "Access Denied"),
        ("connection refused", "Connection Refused"),
    ]
    for pattern, label in error_patterns:
        if pattern in lower_source:
            indicators.append(label)

    return indicators
