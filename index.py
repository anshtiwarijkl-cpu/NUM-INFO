import re
import sys
import time
import logging
import os
import traceback
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request as flask_request

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ==================== PARIVAHAN URLS ====================
HOMEPAGE_URL  = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/statevalidation/homepage.xhtml?statecd=Mzc2MzM2MzAzNjY0MzIzODM3NjIzNjY0MzY2MjM3NDQ0Yw=="
HOMEPAGE_BASE = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/statevalidation/homepage.xhtml"
LOGIN_URL     = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/usermgmt/login.xhtml"
FORM_URL      = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/balanceservice/form_reschedule_fitness.xhtml"

# ==================== SESSION FACTORY ====================

def make_session() -> requests.Session:
    """Create a requests Session with automatic retry on connection errors."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1,          # waits 1s, 2s, 4s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.max_redirects = 10
    return session

# ==================== HEADERS ====================

BASE_HEADERS = {
    "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
}

AJAX_HEADERS = {
    "User-Agent":        BASE_HEADERS["User-Agent"],
    "Accept":            "application/xml, text/xml, */*; q=0.01",
    "Content-Type":      "application/x-www-form-urlencoded; charset=UTF-8",
    "Faces-Request":     "partial/ajax",
    "X-Requested-With":  "XMLHttpRequest",
    "Origin":            "https://vahan.parivahan.gov.in",
    "Accept-Language":   "en-US,en;q=0.9",
    "Accept-Encoding":   "gzip, deflate, br",
    "Connection":        "keep-alive",
    "Sec-Fetch-Dest":    "empty",
    "Sec-Fetch-Mode":    "cors",
    "Sec-Fetch-Site":    "same-origin",
}

# ==================== CHASSIS HELPERS ====================

def extract_last5(chassis: str) -> str | None:
    if not chassis:
        return None
    chassis = str(chassis).strip()
    if chassis.lower() in ("", "null", "none", "n/a"):
        return None
    if "~" in chassis:
        return chassis[-5:]
    clean = re.sub(r"[^A-Z0-9]", "", chassis.upper())
    return clean[-5:] if len(clean) >= 5 else None


def _pick_chassis(data: dict) -> str | None:
    """Return chassis value from any known key in a flat or nested dict."""
    KEYS = ("chassis_number_unmasked", "chassis_number", "chassis_no",
            "chasis_no", "chassisNo", "chassis", "chassisNumber")
    for k in KEYS:
        v = data.get(k)
        if v and str(v).strip().lower() not in ("", "null", "none", "n/a"):
            return str(v).strip()
    if isinstance(data.get("data"), dict):
        for k in KEYS:
            v = data["data"].get(k)
            if v and str(v).strip().lower() not in ("", "null", "none", "n/a"):
                return str(v).strip()
    return None

# ==================== CHASSIS APIS (4 parallel) ====================

def _try_acko(vnum: str) -> str | None:
    r = requests.get(f"https://anuapi.netlify.app/.netlify/functions/api/v2?query={vnum}", timeout=50)
    return _pick_chassis(r.json()) if r.ok else None

def _try_vnum(vnum: str) -> str | None:
    r = requests.get(f"https://vnum-chassis-/vehicle-info?rc={vnum}", timeout=12)
    return _pick_chassis(r.json()) if r.ok else None

def _try_full(vnum: str) -> str | None:
    r = requests.get(f"https://full-chassis-number.vercel.app/acko?vnum={vnum.lower()}", timeout=12)
    return _pick_chassis(r.json()) if r.ok else None

def _try_toxic(vnum: str) -> str | None:
    r = requests.get(f"https://toxic-vehicle-chassis-2x.vercel.app/api?reg={vnum.lower()}", timeout=12)
    return _pick_chassis(r.json()) if r.ok else None


def get_chassis(vnum: str) -> dict:
    """Run all 4 chassis APIs in parallel, return first valid result."""
    apis = [("Acko", _try_acko), ("Vnum", _try_vnum),
            ("Full", _try_full), ("Toxic", _try_toxic)]

    result    = {}
    done      = threading.Event()
    errors    = {}
    err_lock  = threading.Lock()

    def run(name, fn):
        try:
            chassis = fn(vnum)
            if chassis and not done.is_set():
                last5 = extract_last5(chassis)
                if last5:
                    result.update({"chassis_full": chassis, "last5": last5, "source": name})
                    done.set()
                else:
                    with err_lock:
                        errors[name] = f"chassis '{chassis}' has fewer than 5 clean chars"
            elif not chassis:
                with err_lock:
                    errors[name] = "no chassis returned"
        except Exception as e:
            with err_lock:
                errors[name] = str(e)

    threads = [threading.Thread(target=run, args=(n, f), daemon=True) for n, f in apis]
    for t in threads: t.start()
    done.wait(timeout=50)
    for t in threads: t.join(timeout=1)

    if result.get("last5"):
        logger.info(f"Chassis: {result['last5']} (full={result['chassis_full']}, src={result['source']})")
        return {"ok": True, **result}

    return {"ok": False, "error": "All 4 chassis APIs failed", "api_errors": dict(errors)}

# ==================== PARIVAHAN HELPERS ====================

def get_viewstate(html: str) -> str | None:
    tag = BeautifulSoup(html, "html.parser").find("input", {"name": "javax.faces.ViewState"})
    return tag["value"] if tag else None

def get_viewstate_ajax(text: str) -> str | None:
    m = re.search(r'<update id="j_id1:javax\.faces\.ViewState:0"><!\[CDATA\[(.*?)\]\]></update>', text)
    return m.group(1) if m else None

def get_checkbox(html: str) -> str:
    m = re.search(r'id="(j_idt\d+)"[^>]*class="[^"]*ui-chkbox', html)
    return m.group(1) if m else "j_idt187"

# ==================== PARIVAHAN FLOW ====================

def parivahan_fetch(vnum: str, last5: str) -> dict:
    """
    Full 10-step Parivahan session.
    Auto-retried up to 3 times on ConnectionError/Timeout (handled by caller).
    Returns {"ok": True, "mobile": "..."} or {"ok": False, "error": "...", ...}
    """
    session = make_session()
    bh = BASE_HEADERS.copy()
    ah = AJAX_HEADERS.copy()

    # Step 1: Homepage
    logger.info("Step1-Homepage")
    r1 = session.get(HOMEPAGE_URL, headers=bh, timeout=30)
    if r1.status_code != 200:
        return {"ok": False, "error": f"Step1: HTTP {r1.status_code}", "snippet": r1.text[:500]}

    vs  = get_viewstate(r1.text)
    chk = get_checkbox(r1.text)
    if not vs:
        return {"ok": False, "error": "Step1: ViewState missing", "snippet": r1.text[:500]}
    logger.info(f"  checkbox={chk}, vs_len={len(vs)}")

    # Step 2: Select RTO
    logger.info("Step2-SelectRTO")
    ah["Referer"] = HOMEPAGE_URL
    r2 = session.post(HOMEPAGE_BASE, headers=ah, timeout=30, data={
        "javax.faces.partial.ajax": "true", "javax.faces.source": "fit_c_office_to",
        "javax.faces.partial.execute": "fit_c_office_to",
        "javax.faces.behavior.event": "change", "javax.faces.partial.event": "change",
        "homepageformid": "homepageformid", "j_idt12": "", "j_idt47_input": "en",
        "state_cd_filter": "", "fit_c_office_to_input": "1", "abc": "abc",
        "javax.faces.ViewState": vs, "pmtchk_input": "-1", "nocregnno": "",
    })
    vs = get_viewstate_ajax(r2.text) or vs

    # Step 3: Checkbox
    logger.info("Step3-Checkbox")
    r3 = session.post(HOMEPAGE_BASE, headers=ah, timeout=30, data={
        "javax.faces.partial.ajax": "true", "javax.faces.source": chk,
        "javax.faces.partial.execute": chk, "javax.faces.partial.render": "proccedHomeButtonId",
        "javax.faces.behavior.event": "change", "javax.faces.partial.event": "change",
        "homepageformid": "homepageformid", "j_idt12": "", "j_idt47_input": "en",
        "state_cd_filter": "", "fit_c_office_to_input": "1", f"{chk}_input": "on",
        "abc": "abc", "javax.faces.ViewState": vs, "pmtchk_input": "-1", "nocregnno": "",
    })
    vs = get_viewstate_ajax(r3.text) or vs

    # Step 4: Proceed
    logger.info("Step4-Proceed")
    r4 = session.post(HOMEPAGE_BASE, headers=ah, timeout=30, data={
        "javax.faces.partial.ajax": "true", "javax.faces.source": "proccedHomeButtonId",
        "javax.faces.partial.execute": "@all",
        "javax.faces.partial.render": "regnid facelesslist portaldownMsgPnl mainhomepagepnl leftmenupnlid leftmenupnlidservdown",
        "proccedHomeButtonId": "proccedHomeButtonId", "homepageformid": "homepageformid",
        "j_idt12": "", "j_idt47_input": "en", "state_cd_filter": "",
        "fit_c_office_to_input": "1", f"{chk}_input": "on", "abc": "abc",
        "javax.faces.ViewState": vs, "pmtchk_input": "-1", "nocregnno": "",
    })
    vs = get_viewstate_ajax(r4.text) or vs

    # Step 5: Dialog button
    logger.info("Step5-Dialog")
    dm  = re.search(r'id="(j_idt\d+)"[^>]*class="[^"]*ui-button', r4.text)
    dbt = dm.group(1) if dm else "j_idt536"
    r5  = session.post(HOMEPAGE_BASE, headers=ah, timeout=30, data={
        "javax.faces.partial.ajax": "true", "javax.faces.source": dbt,
        "javax.faces.partial.execute": "@all", f"{dbt}": dbt,
        "homepageformid": "homepageformid", "j_idt12": "", "j_idt47_input": "en",
        "state_cd_filter": "", "fit_c_office_to_input": "1", f"{chk}_input": "on",
        "pmtchk_input": "-1", "nocregnno": "", "javax.faces.ViewState": vs,
    })
    vs = get_viewstate_ajax(r5.text) or vs

    # Step 6: Login page
    logger.info("Step6-Login")
    lh = {**bh, "Referer": HOMEPAGE_URL}
    r6 = session.get(LOGIN_URL + "?faces-redirect=true", headers=lh, timeout=30, allow_redirects=True)
    vs = get_viewstate(r6.text)
    if not vs:
        return {"ok": False, "error": "Step6: ViewState missing on login page", "snippet": r6.text[:500]}

    # Step 7: fitbalcTest
    logger.info("Step7-fitbalcTest")
    fm  = re.search(r'id="(j_idt\d+)"[^>]*name="\1"[^>]*type="submit"', r6.text)
    fbt = fm.group(1) if fm else "j_idt506"
    ph  = {**bh, "Content-Type": "application/x-www-form-urlencoded",
           "Origin": "https://vahan.parivahan.gov.in",
           "Referer": LOGIN_URL + "?faces-redirect=true"}
    r7  = session.post(LOGIN_URL, headers=ph, timeout=30, allow_redirects=True, data={
        "loginForm": "loginForm", f"{fbt}": fbt,
        "javax.faces.ViewState": vs, "InputEnter": "",
        "fitbalcTest": "fitbalcTest", "pur_cd": "86",
    })

    # Step 8: Form page
    logger.info("Step8-Form")
    fh = {**bh, "Referer": LOGIN_URL + "?faces-redirect=true", "Cache-Control": "max-age=0"}
    r8 = session.get(FORM_URL, headers=fh, timeout=30)
    vs = get_viewstate(r8.text)
    if not vs:
        return {"ok": False, "error": "Step8: ViewState missing on form page", "snippet": r8.text[:500]}
    logger.info(f"  form page len={len(r8.text)}")

    # Step 9: Submit
    logger.info(f"Step9-Submit: vnum={vnum} chassis={last5}")
    ah["Referer"] = FORM_URL
    r9 = session.post(FORM_URL, headers=ah, timeout=30, data={
        "javax.faces.partial.ajax": "true",
        "javax.faces.source": "balanceFeesFine:validate_dtls",
        "javax.faces.partial.execute": "@all",
        "javax.faces.partial.render": "balanceFeesFine:auth_panel",
        "balanceFeesFine:validate_dtls": "balanceFeesFine:validate_dtls",
        "balanceFeesFine": "balanceFeesFine",
        "balanceFeesFine:tf_reg_no": vnum,
        "balanceFeesFine:tf_chasis_no": last5,
        "javax.faces.ViewState": vs,
    })

    # Step 10: Extract mobile
    logger.info("Step10-Extract")
    body = r9.text
    for pat in [
        r'id="balanceFeesFine:tf_mobile"[^>]*value="(\d{10})"',
        r'value="(\d{10})"[^>]*id="balanceFeesFine:tf_mobile"',
        r'balanceFeesFine:tf_mobile[^>]*value="(\d{10})"',
    ]:
        m = re.search(pat, body, re.DOTALL)
        if m and m.group(1)[0] in "6789":
            logger.info(f"Mobile found: {m.group(1)}")
            return {"ok": True, "mobile": m.group(1)}

    hits = re.findall(r"\b([6-9]\d{9})\b", body)
    if hits:
        logger.info(f"Mobile found (fallback): {hits[0]}")
        return {"ok": True, "mobile": hits[0]}

    logger.warning(f"Mobile not found. response_len={len(body)}")
    return {
        "ok":      False,
        "error":   "Mobile not found in Parivahan response",
        "snippet": body[:800],
    }

# ==================== CORE LOOKUP ====================

def lookup(raw: str) -> dict:
    vnum = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(vnum) < 6:
        return {"success": False, "error": "Vehicle number too short (min 6 chars)", "input": raw}

    # 1. Get chassis (parallel)
    cr = get_chassis(vnum)
    if not cr["ok"]:
        return {
            "success":    False,
            "vehicle":    vnum,
            "error":      cr["error"],
            "api_errors": cr.get("api_errors", {}),
        }

    last5          = cr["last5"]
    chassis_full   = cr["chassis_full"]
    chassis_source = cr["source"]

    # 2. Parivahan — retry up to 3 times on connection errors
    last_err = {}
    for attempt in range(1, 4):
        logger.info(f"Parivahan attempt {attempt}/3")
        try:
            mr = parivahan_fetch(vnum, last5)
            if mr["ok"]:
                return {
                    "success":        True,
                    "vehicle":        vnum,
                    "mobile":         mr["mobile"],
                    "chassis_last5":  last5,
                    "chassis_full":   chassis_full,
                    "chassis_source": chassis_source,
                }
            # soft failure (mobile not found in page) — retry too
            last_err = mr
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            logger.warning(f"Attempt {attempt} ConnectionError: {e}")
            last_err = {"error": str(e), "type": "ConnectionError"}
            time.sleep(attempt * 1.5)   # 1.5s, 3s, 4.5s back-off
            continue
        except requests.exceptions.Timeout:
            logger.warning(f"Attempt {attempt} Timeout")
            last_err = {"error": "Request timed out", "type": "Timeout"}
            time.sleep(attempt * 1.5)
            continue
        except Exception as e:
            last_err = {"error": str(e), "type": "Exception", "traceback": traceback.format_exc()}
            break

        # small pause before retry on soft failure
        if attempt < 3:
            time.sleep(1)

    return {
        "success":        False,
        "vehicle":        vnum,
        "error":          last_err.get("error", "Unknown error"),
        "detail":         last_err.get("type", ""),
        "snippet":        last_err.get("snippet", ""),
        "chassis_last5":  last5,
        "chassis_full":   chassis_full,
        "chassis_source": chassis_source,
    }

# ==================== FLASK ====================

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({
        "status": "ok",
        "endpoints": {
            "GET /vehicle/<reg>": "Lookup mobile number",
            "POST /bulk":         "Bulk lookup  body: {\"vehicles\":[...]}",
            "GET /health":        "Health check",
        }
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/vehicle/<reg>")
def get_vehicle(reg):
    try:
        res = lookup(reg)
        code = 200 if res["success"] else 422
        return jsonify(res), code
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500

@app.route("/bulk", methods=["POST"])
def bulk():
    try:
        body = flask_request.get_json(force=True, silent=True) or {}
        vehicles = [re.sub(r"[^A-Z0-9]", "", v.upper()) for v in body.get("vehicles", []) if v]
        vehicles = [v for v in vehicles if len(v) >= 6]
        if not vehicles:
            return jsonify({"success": False, "error": "No valid vehicle numbers"}), 400
        if len(vehicles) > 5:
            return jsonify({"success": False, "error": "Max 5 per request"}), 400

        results = {}
        def do(v):
            try:
                results[v] = lookup(v)
            except Exception as e:
                results[v] = {"success": False, "error": str(e)}

        threads = [threading.Thread(target=do, args=(v,), daemon=True) for v in vehicles]
        for t in threads: t.start()
        for t in threads: t.join(timeout=120)
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500

# ==================== ENTRY ====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2002))
    logger.info(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
