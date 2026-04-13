"""
EOL Checker Lambda — checks software end-of-life status using endoflife.date API.

Configuration is loaded from an S3 JSON file so products can be updated
without redeploying the Lambda. Alerts are sent via SNS (email).

Environment variables:
    CONFIG_BUCKET  — S3 bucket containing the config file
    CONFIG_KEY     — S3 key for the config file (default: eol_config.json)
    SNS_TOPIC_ARN  — SNS topic ARN for email notifications
"""

import json
import logging
import os
import urllib.request
import urllib.error
from datetime import date, datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EOL_API_BASE = "https://endoflife.date/api"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config_from_s3():
    """Load product configuration from S3."""
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    key = os.environ.get("CONFIG_KEY", "eol_config.json")
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def load_config_from_file(path):
    """Load product configuration from a local file (for testing)."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def fetch_all_cycles(product):
    """Fetch all release cycles for a product from endoflife.date.

    Returns the list sorted newest-first (as the API provides), or None on error.
    """
    url = f"{EOL_API_BASE}/{product}.json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("API error for %s: %s %s", product, exc.code, exc.reason)
        return None
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", product, exc)
        return None


def parse_date_field(value):
    """Parse an EOL/support field which can be a date string, bool, or None.

    Returns:
        date   — if a valid date string was provided
        True   — already EOL / support ended (no specific date)
        False  — no EOL planned / still supported
        None   — field missing or unparseable
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def check_product(entry, today):
    """Check a single product entry and return a status dict.

    Fetches all cycles once per product to extract:
      - tracked cycle info (EOL, latest patch, patch release date)
      - latest available cycle (newest major/minor version)
    """
    product = entry["product"]
    version = entry["version"]
    label = entry.get("label", f"{product} {version}")

    error_result = {
        "label": label,
        "product": product,
        "version": version,
        "status": "error",
        "days_remaining": None,
    }

    cycles = fetch_all_cycles(product)
    if cycles is None:
        return {**error_result, "message": "Failed to fetch data from API"}

    # Find the tracked cycle
    info = None
    for c in cycles:
        if str(c.get("cycle")) == str(version):
            info = c
            break

    if info is None:
        available = [c.get("cycle") for c in cycles[:6]]
        return {
            **error_result,
            "message": f"Cycle '{version}' not found. Available: {available}",
        }

    # --- Extract tracked cycle fields ---
    eol = parse_date_field(info.get("eol"))
    support = parse_date_field(info.get("support"))
    latest_patch = info.get("latest", "unknown")
    latest_patch_date = info.get("latestReleaseDate")
    lts = info.get("lts", False)

    # --- Latest available cycle (first in list = newest) ---
    newest = cycles[0]
    latest_cycle = newest.get("cycle")
    latest_cycle_version = newest.get("latest", latest_cycle)
    latest_cycle_release_date = newest.get("releaseDate")
    on_latest_cycle = str(latest_cycle) == str(version)

    result = {
        "label": label,
        "product": product,
        "version": version,
        "lts": lts,
        # Tracked cycle — latest patch
        "latest_patch": latest_patch,
        "latest_patch_date": latest_patch_date,
        # Latest available cycle
        "latest_cycle": latest_cycle,
        "latest_cycle_version": latest_cycle_version,
        "latest_cycle_release_date": latest_cycle_release_date,
        "on_latest_cycle": on_latest_cycle,
        # EOL / support
        "eol_date": str(eol) if isinstance(eol, date) else None,
        "support_date": str(support) if isinstance(support, date) else None,
        "days_remaining": None,
        "support_days_remaining": None,
    }

    # --- EOL status ---
    if eol is True:
        result["status"] = "eol"
        result["message"] = "Already end of life (no specific date)"
    elif eol is False:
        result["status"] = "ok"
        result["message"] = "No EOL date announced — still supported"
    elif isinstance(eol, date):
        days = (eol - today).days
        result["days_remaining"] = days
        if days < 0:
            result["status"] = "eol"
            result["message"] = f"EOL since {eol} ({abs(days)} days ago)"
        elif days == 0:
            result["status"] = "eol"
            result["message"] = f"Reaches end of life TODAY ({eol})"
        else:
            result["status"] = "approaching"
            result["message"] = f"EOL on {eol} ({days} days remaining)"
    else:
        result["status"] = "unknown"
        result["message"] = f"Could not determine EOL status (raw value: {info.get('eol')})"

    # --- Active support status (informational) ---
    if isinstance(support, date):
        support_days = (support - today).days
        result["support_days_remaining"] = support_days
        if support_days < 0:
            result["support_message"] = f"Active support ended {support} ({abs(support_days)} days ago)"
        else:
            result["support_message"] = f"Active support until {support} ({support_days} days remaining)"

    return result


# ---------------------------------------------------------------------------
# Categorisation (shared by both formatters)
# ---------------------------------------------------------------------------

def _categorise(results, thresholds):
    """Split results into eol / approaching / ok / error buckets."""
    max_threshold = max(thresholds) if thresholds else 90
    eol, approaching, ok, errors = [], [], [], []

    for r in results:
        status = r["status"]
        if status == "error":
            errors.append(r)
        elif status == "eol":
            eol.append(r)
        elif status == "approaching" and r["days_remaining"] is not None and r["days_remaining"] <= max_threshold:
            approaching.append(r)
        else:
            ok.append(r)

    approaching.sort(key=lambda x: x["days_remaining"])
    return eol, approaching, ok, errors, max_threshold


# ---------------------------------------------------------------------------
# Plain-text report
# ---------------------------------------------------------------------------

def _append_version_info(lines, r):
    """Append latest-patch and latest-cycle lines to the report."""
    if r.get("latest_patch"):
        patch_line = f"    Latest patch: {r['latest_patch']}"
        if r.get("latest_patch_date"):
            patch_line += f" (released {r['latest_patch_date']})"
        lines.append(patch_line)

    if r.get("latest_cycle"):
        if r.get("on_latest_cycle"):
            cycle_line = f"    Latest cycle: {r['latest_cycle']} (you are on the latest)"
        else:
            cycle_line = f"    Latest cycle: {r['latest_cycle']} -> {r.get('latest_cycle_version', '?')}"
            if r.get("latest_cycle_release_date"):
                cycle_line += f" (released {r['latest_cycle_release_date']})"
        lines.append(cycle_line)


def format_report_text(results, thresholds, today):
    """Format results into a readable plain-text report.

    Returns (report_text, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, max_t = _categorise(results, thresholds)

    lines = [
        f"End-of-Life Status Report  -  {today}",
        "=" * 52,
    ]

    has_alerts = bool(eol_items or approaching_items)

    if eol_items:
        lines += ["", "!! ALREADY END OF LIFE", "-" * 42]
        for r in eol_items:
            lines.append(f"  * {r['label']}")
            lines.append(f"    {r['message']}")
            _append_version_info(lines, r)

    if approaching_items:
        lines += ["", f">> APPROACHING END OF LIFE (within {max_t} days)", "-" * 42]
        for r in approaching_items:
            lines.append(f"  * {r['label']}")
            lines.append(f"    {r['message']}")
            if r.get("support_message"):
                lines.append(f"    {r['support_message']}")
            _append_version_info(lines, r)

    if ok_items:
        lines += ["", "-- No Immediate Concerns", "-" * 42]
        for r in ok_items:
            lines.append(f"  * {r['label']}  -  {r['message']}")
            _append_version_info(lines, r)

    if error_items:
        lines += ["", "?? Errors", "-" * 42]
        for r in error_items:
            lines.append(f"  * {r['label']}  -  {r['message']}")

    lines += [
        "",
        "=" * 52,
        f"Source: endoflife.date  |  Products checked: {len(results)}",
    ]

    return "\n".join(lines), has_alerts


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_STATUS_COLOURS = {
    "eol":         {"bg": "#fce4e4", "badge_bg": "#d32f2f", "badge_text": "#fff"},
    "approaching": {"bg": "#fff8e1", "badge_bg": "#f57c00", "badge_text": "#fff"},
    "ok":          {"bg": "#e8f5e9", "badge_bg": "#388e3c", "badge_text": "#fff"},
    "error":       {"bg": "#f5f5f5", "badge_bg": "#757575", "badge_text": "#fff"},
}


def _badge(status_key, label):
    c = _STATUS_COLOURS.get(status_key, _STATUS_COLOURS["error"])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:3px;'
        f'font-size:12px;font-weight:bold;color:{c["badge_text"]};'
        f'background-color:{c["badge_bg"]}">{label}</span>'
    )


def _cycle_cell(r):
    if not r.get("latest_cycle"):
        return "-"
    if r.get("on_latest_cycle"):
        return f'{r["latest_cycle"]} (latest)'
    text = f'{r["latest_cycle"]} &rarr; {r.get("latest_cycle_version", "?")}'
    if r.get("latest_cycle_release_date"):
        text += f'<br><span style="color:#888;font-size:12px">released {r["latest_cycle_release_date"]}</span>'
    return text


def _status_label(r, bucket):
    """Render a coloured badge. *bucket* is the category the item was placed in
    (eol / approaching / ok / error), which may differ from r['status'] when a
    product is 'approaching' but beyond the configured threshold."""
    if bucket == "eol":
        return _badge("eol", "END OF LIFE")
    if bucket == "approaching":
        return _badge("approaching", f'{r["days_remaining"]}d remaining')
    if bucket == "ok":
        if r.get("days_remaining") is not None:
            return _badge("ok", f'{r["days_remaining"]}d remaining')
        return _badge("ok", "OK")
    return _badge("error", "ERROR")


def _html_table_rows(items, status_key):
    """Generate <tr> elements for a list of result items."""
    bg = _STATUS_COLOURS.get(status_key, _STATUS_COLOURS["error"])["bg"]
    rows = []
    for r in items:
        patch = r.get("latest_patch", "-")
        if r.get("latest_patch_date"):
            patch += f'<br><span style="color:#888;font-size:12px">released {r["latest_patch_date"]}</span>'

        eol_display = r.get("eol_date") or "-"

        rows.append(
            f'<tr style="background-color:{bg}">'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0;font-weight:bold">{r["label"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_status_label(r, status_key)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{r["message"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{eol_display}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{patch}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_cycle_cell(r)}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def format_report_html(results, thresholds, today):
    """Format results into an inline-styled HTML report.

    Returns (html_string, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, max_t = _categorise(results, thresholds)
    has_alerts = bool(eol_items or approaching_items)

    # Summary banner
    if eol_items and approaching_items:
        banner_text = f"{len(eol_items)} EOL + {len(approaching_items)} approaching"
        banner_bg = "#d32f2f"
    elif eol_items:
        banner_text = f"{len(eol_items)} product(s) past end of life"
        banner_bg = "#d32f2f"
    elif approaching_items:
        banner_text = f"{len(approaching_items)} product(s) approaching end of life"
        banner_bg = "#f57c00"
    else:
        banner_text = "All products are within support"
        banner_bg = "#388e3c"

    # Build table rows in status order
    all_rows = ""
    if eol_items:
        all_rows += _html_table_rows(eol_items, "eol")
    if approaching_items:
        all_rows += _html_table_rows(approaching_items, "approaching")
    if ok_items:
        all_rows += _html_table_rows(ok_items, "ok")
    if error_items:
        all_rows += _html_table_rows(error_items, "error")

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#f4f4f4;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f4">
<tr><td align="center" style="padding:24px 12px">

  <!-- Container -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:900px;background-color:#ffffff;border-radius:8px;overflow:hidden">

    <!-- Header -->
    <tr>
      <td style="background-color:#1a237e;color:#ffffff;padding:20px 24px">
        <h1 style="margin:0;font-size:22px;font-weight:bold">End-of-Life Status Report</h1>
        <p style="margin:6px 0 0;font-size:14px;color:#c5cae9">{today} &nbsp;|&nbsp; {len(results)} products checked</p>
      </td>
    </tr>

    <!-- Banner -->
    <tr>
      <td style="background-color:{banner_bg};color:#ffffff;padding:12px 24px;font-size:15px;font-weight:bold">
        {banner_text}
      </td>
    </tr>

    <!-- Table -->
    <tr>
      <td style="padding:0">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
          <tr style="background-color:#263238">
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Product</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Status</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Details</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">EOL Date</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Latest Patch</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Latest Cycle</th>
          </tr>
          {all_rows}
        </table>
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="padding:16px 24px;font-size:12px;color:#888888;border-top:1px solid #e0e0e0">
        Source: <a href="https://endoflife.date" style="color:#1a237e">endoflife.date</a>
        &nbsp;|&nbsp; Alert threshold: {max_t} days
      </td>
    </tr>

  </table>

</td></tr>
</table>
</body>
</html>"""

    return html, has_alerts


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _notify_console(report_text, **_kwargs):
    """Print the plain-text report to stdout."""
    print(report_text)


def _notify_html_file(report_html, notif_config, **_kwargs):
    """Write the HTML report to a local file."""
    path = notif_config.get("path", "eol_report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_html)
    logger.info("HTML report written to %s", path)


def _notify_sns(report_text, subject, notif_config, **_kwargs):
    """Publish the plain-text report to an SNS topic."""
    import boto3

    topic_arn = notif_config.get("topic_arn") or os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        logger.error("SNS notification skipped: no topic_arn in config or SNS_TOPIC_ARN env var")
        return
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report_text)
    logger.info("SNS notification sent to %s", topic_arn)


def _notify_ses(report_html, subject, notif_config, **_kwargs):
    """Send the HTML report as an email via SES."""
    import boto3

    from_email = notif_config.get("from_email") or os.environ.get("SES_FROM_EMAIL")
    to_emails = notif_config.get("to_emails") or []
    env_to = os.environ.get("SES_TO_EMAILS")
    if env_to and not to_emails:
        to_emails = [e.strip() for e in env_to.split(",")]

    if not from_email or not to_emails:
        logger.error("SES notification skipped: from_email or to_emails not configured")
        return

    ses = boto3.client("ses")
    ses.send_email(
        Source=from_email,
        Destination={"ToAddresses": to_emails},
        Message={
            "Subject": {"Data": subject[:100], "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": report_html, "Charset": "UTF-8"},
            },
        },
    )
    logger.info("SES email sent from %s to %s", from_email, to_emails)


_NOTIFIERS = {
    "console":   _notify_console,
    "html_file": _notify_html_file,
    "sns":       _notify_sns,
    "ses":       _notify_ses,
}


def send_notifications(config, report_text, report_html, subject):
    """Dispatch the report to every notification channel listed in config."""
    notifications = config.get("notifications", [{"type": "sns"}])

    for notif in notifications:
        ntype = notif.get("type")
        handler = _NOTIFIERS.get(ntype)
        if handler is None:
            logger.warning("Unknown notification type: %s — skipping", ntype)
            continue
        try:
            handler(
                report_text=report_text,
                report_html=report_html,
                subject=subject,
                notif_config=notif,
            )
        except Exception as exc:
            logger.error("Notification '%s' failed: %s", ntype, exc)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Entry point for AWS Lambda."""
    today = date.today()

    config = load_config_from_s3()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])
    notify_when = config.get("notify_when", "always")  # "always" | "alerts_only"

    logger.info("Checking %d products for EOL status", len(products))

    results = [check_product(entry, today) for entry in products]

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    should_notify = notify_when == "always" or has_alerts

    if should_notify:
        prefix = "EOL ALERT" if has_alerts else "EOL Report"
        subject = f"[{prefix}] Software End-of-Life Status - {today}"
        send_notifications(config, report_text, report_html, subject)
    else:
        logger.info("No alerts and notify_when=alerts_only — skipping notification")

    return {
        "statusCode": 200,
        "checked": len(results),
        "has_alerts": has_alerts,
        "notified": should_notify,
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "eol_config.json"
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results = [check_product(entry, today) for entry in products]
    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    prefix = "EOL ALERT" if has_alerts else "EOL Report"
    subject = f"[{prefix}] Software End-of-Life Status - {today}"

    send_notifications(config, report_text, report_html, subject)
