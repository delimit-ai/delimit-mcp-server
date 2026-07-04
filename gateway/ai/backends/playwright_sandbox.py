import sys
import json
import argparse
import shutil
from typing import Dict, Any, List

def run_responsive_check(url_or_path: str, check_types: List[str]) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError
    except ImportError:
        return {"status": "error", "error": "Playwright is not installed in the subprocess environment."}
    
    results: Dict[str, Any] = {
        "breakpoints_tested": [],
        "issues": [],
        "engine": "playwright",
        "status": "ok"
    }
    
    with sync_playwright() as p:
        browser = None
        executable_path = None
        try:
            # Attempt to use Playwright's managed Chromium
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            # Fallback to system Chromium
            for exe in ["chromium", "chromium-browser", "google-chrome"]:
                found = shutil.which(exe)
                if found:
                    executable_path = found
                    break
            
            if not executable_path:
                return {"status": "error", "error": f"Failed to launch Playwright Chromium and no system fallback found. Original error: {e}"}
            
            try:
                browser = p.chromium.launch(headless=True, executable_path=executable_path)
            except Exception as fallback_err:
                return {"status": "error", "error": f"Failed to launch with system Chromium. Error: {fallback_err}"}

        if not browser:
            return {"status": "error", "error": "Browser could not be initialized."}

        context = browser.new_context()
        page = context.new_page()

        breakpoints = {
            "sm": 640,
            "md": 768,
            "lg": 1024,
            "xl": 1280,
            "2xl": 1536
        }

        try:
            # If path doesn't start with http, assume local file
            if not url_or_path.startswith("http://") and not url_or_path.startswith("https://") and not url_or_path.startswith("file://"):
                url = f"file://{url_or_path}"
            else:
                url = url_or_path

            page.goto(url, wait_until="load", timeout=15000)
            
            for bp_name, width in breakpoints.items():
                if check_types and bp_name not in check_types:
                    continue
                
                page.set_viewport_size({"width": width, "height": 800})
                page.wait_for_timeout(200)
                
                scroll_width = page.evaluate("document.documentElement.scrollWidth")
                client_width = page.evaluate("document.documentElement.clientWidth")
                
                results["breakpoints_tested"].append(bp_name)
                
                if scroll_width > client_width:
                    results["issues"].append({
                        "severity": "error",
                        "message": f"Horizontal overflow at {bp_name} ({width}px): scrollWidth ({scroll_width}px) > clientWidth ({client_width}px)",
                        "fix": "Check for elements with fixed width exceeding the viewport or missing max-width: 100%"
                    })
        except TimeoutError:
            results["issues"].append({"severity": "warning", "message": f"Timeout loading {url}"})
        except Exception as e:
            results["issues"].append({"severity": "error", "message": f"Error during Playwright test: {e}"})
        finally:
            browser.close()
            
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-or-path", required=True)
    parser.add_argument("--check-types", nargs="*", default=[])
    args = parser.parse_args()
    
    try:
        res = run_responsive_check(args.url_or_path, args.check_types)
        print(json.dumps(res))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
