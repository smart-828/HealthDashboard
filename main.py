import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import pdfplumber
from PIL import Image
import re
import urllib.parse
import requests
import base64
import json
import io

# ─────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Patrick's Health Dashboard", layout="wide")
st.markdown("""
<style>
[data-testid="stMetricLabel"]  { font-size:0.82rem!important; color:#888!important; }
[data-testid="stMetricValue"]  { font-size:1.5rem!important; font-weight:700!important; }
.block-container { padding-top:2rem!important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  CONSTANTS – PROFILE
# ─────────────────────────────────────────────
DOB        = date(1964, 12, 1)
HEIGHT_CM  = 175
ETHNICITY  = "asian"   # drives BMI / visceral fat thresholds

def age_today() -> int:
    today = date.today()
    return today.year - DOB.year - ((today.month, today.day) < (DOB.month, DOB.day))

# ─────────────────────────────────────────────
#  BENCHMARKS  (auto-adjust by age each run)
# ─────────────────────────────────────────────
def get_benchmarks() -> dict:
    age = age_today()
    # Blood pressure (universal, UK/international)
    bp = dict(optimal_sys=120, optimal_dia=80,
              caution_sys=130, caution_dia=85,
              high_sys=140,    high_dia=90)
    # Resting heart rate (universal)
    hr = dict(low=40, optimal_low=50, optimal_high=70, caution_high=90, high=100)
    # BMI – Asian-specific WHO cutoffs
    bmi = dict(underweight=18.5, normal_low=18.5, normal_high=22.9,
               overweight=23.0, obese=27.5)
    # Visceral fat – Asian-specific (risk starts lower)
    vf = dict(optimal=9.0, caution=12.0, high=15.0)
    # Body fat % – age & gender adjusted (male)
    if age < 40:
        bf = dict(low=8, normal_low=11, normal_high=21, high=25)
    elif age < 60:
        bf = dict(low=11, normal_low=14, normal_high=23, high=27)
    else:
        bf = dict(low=13, normal_low=16, normal_high=25, high=30)
    return dict(bp=bp, hr=hr, bmi=bmi, vf=vf, bf=bf)

def bp_status(sys, dia) -> str:
    b = get_benchmarks()["bp"]
    if sys >= b["high_sys"]    or dia >= b["high_dia"]:    return "⚠️ High"
    if sys >= b["caution_sys"] or dia >= b["caution_dia"]: return "🟡 Caution"
    return "✅ Optimal"

def hr_status(hr_val) -> str:
    b = get_benchmarks()["hr"]
    if hr_val < b["low"] or hr_val > b["high"]:       return "⚠️ Attention"
    if hr_val > b["caution_high"]:                    return "🟡 Caution"
    if b["optimal_low"] <= hr_val <= b["optimal_high"]: return "✅ Optimal"
    return "🟡 Borderline"

def bmi_status(bmi_val) -> str:
    b = get_benchmarks()["bmi"]
    if bmi_val < b["underweight"]: return "⚠️ Underweight"
    if bmi_val <= b["normal_high"]: return "✅ Normal (Asian)"
    if bmi_val < b["obese"]:       return "🟡 Overweight (Asian)"
    return "⚠️ Obese (Asian)"

def vf_status(vf_val) -> str:
    b = get_benchmarks()["vf"]
    if vf_val <= b["optimal"]:  return "✅ Optimal"
    if vf_val <= b["caution"]:  return "🟡 Caution"
    return "⚠️ High"

def bf_status(bf_val) -> str:
    b = get_benchmarks()["bf"]
    if bf_val < b["low"]:          return "⚠️ Too Low"
    if bf_val <= b["normal_high"]: return "✅ Normal"
    if bf_val <= b["high"]:        return "🟡 High"
    return "⚠️ Very High"

# ─────────────────────────────────────────────
#  GOOGLE SHEETS – READ (public CSV export)
# ─────────────────────────────────────────────
SHEET_ID = "1N027WgDwc2yPGI7m69eBo23yOPhSs55JudVgejcpDbQ"

def sheet_csv_url(sheet_name: str) -> str:
    return (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
            f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}")

@st.cache_data(ttl=60)
def load_logs() -> pd.DataFrame:
    ALL_COLS = [
        "datetime", "date", "source",
        # Blood pressure
        "sys_mmHg", "dia_mmHg", "pulse_bpm",
        # ECG
        "heart_rate_bpm", "determination",
        # Scale – primary
        "weight_kg", "bmi", "body_fat_pct", "fat_mass_kg",
        "fat_free_weight_kg", "muscle_mass_kg", "muscle_rate_pct",
        "skeletal_muscle_pct", "bone_mass_kg", "protein_mass_kg",
        "protein_pct", "water_weight_kg", "body_water_pct",
        "subcutaneous_fat_pct", "visceral_fat", "bmr_kcal",
        "body_age", "ideal_weight_kg", "obesity_level",
        "body_type", "heart_rate_scale_bpm", "cardiac_index",
        # Status
        "status"
    ]
    try:
        df = pd.read_csv(sheet_csv_url("logs"))
        if df.empty:
            return pd.DataFrame(columns=ALL_COLS)
        # ensure all expected columns exist
        for c in ALL_COLS:
            if c not in df.columns:
                df[c] = None
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        return df
    except Exception:
        return pd.DataFrame(columns=ALL_COLS)

@st.cache_data(ttl=300)
def load_profile() -> int:
    try:
        df = pd.read_csv(sheet_csv_url("profile"))
        if not df.empty and "height_cm" in df.columns:
            return int(df["height_cm"].iloc[0])
    except Exception:
        pass
    return HEIGHT_CM

# ─────────────────────────────────────────────
#  GOOGLE SHEETS – WRITE  (Apps Script web app)
# ─────────────────────────────────────────────
def save_row(row: dict, worksheet: str = "logs") -> bool:
    """
    Appends a row to Google Sheets via a deployed Apps Script Web App.
    The Apps Script URL must be stored in secrets as:
      [gsheets]
      apps_script_url = "https://script.google.com/macros/s/.../exec"
    """
    try:
        url = st.secrets["gsheets"]["apps_script_url"]
        payload = {"worksheet": worksheet, "row": row}
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            st.cache_data.clear()
            return True
        st.error(f"Save failed (HTTP {r.status_code}): {r.text}")
        return False
    except KeyError:
        st.warning("⚙️ Google Sheets write not configured yet. "
                   "Add apps_script_url to secrets.toml to enable saving.")
        return False
    except Exception as e:
        st.error(f"Save error: {e}")
        return False

# ─────────────────────────────────────────────
#  DUPLICATE CHECK
# ─────────────────────────────────────────────
def is_duplicate(row: dict, df: pd.DataFrame) -> bool:
    source = row.get("source", "")
    if df.empty or "source" not in df.columns:
        return False
    if source == "kardia_pdf":
        # use datetime (date + time) for ECG – same day different time = different record
        if "datetime" not in df.columns:
            return False
        return row.get("datetime", "") in df["datetime"].astype(str).values
    else:
        # use date only for scale and manual BP
        row_date = str(row.get("date", ""))[:10]
        same_source = df[df["source"] == source]
        if same_source.empty:
            return False
        existing_dates = same_source["date"].astype(str).str[:10].values
        return row_date in existing_dates

# ─────────────────────────────────────────────
#  PDF EXTRACTION  – Kardia 6L
# ─────────────────────────────────────────────
def extract_kardia_pdf(file) -> dict | None:
    try:
        with pdfplumber.open(file) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages[:2])

        # Heart rate
        hr_match = re.search(r'Heart\s+Rate[:\s]+([\d]+)\s*BPM', text, re.IGNORECASE)
        hr = int(hr_match.group(1)) if hr_match else None

        # Determination
        determination = "Unknown"
        for det in ["Normal Sinus Rhythm", "Bradycardia", "Tachycardia",
                    "Unclassified", "Possible Atrial Fibrillation",
                    "Normal", "No Analysis"]:
            if det.lower() in text.lower():
                determination = det
                break

        # Date & time – "Recorded on: Heart Rate:\nMonday, 22 Jul 2024, 4:56pm 54 BPM"
        # Date is on the line AFTER "Recorded on:"
        recorded_date     = datetime.now().strftime("%Y-%m-%d")
        recorded_datetime = datetime.now().strftime("%Y-%m-%d %H:%M")
        dt_match = re.search(
            r'Recorded\s+on:.*?\n'
            r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
            r'(\d{1,2}\s+\w+\s+\d{4},?\s*\d{1,2}:\d{2}\s*(?:am|pm)?)',
            text, re.IGNORECASE)
        if dt_match:
            raw = dt_match.group(1).strip().rstrip(",")
            for fmt in ("%d %b %Y, %I:%M %p", "%d %B %Y, %I:%M %p",
                        "%d %b %Y %I:%M%p",   "%d %b %Y, %I:%M%p",
                        "%d %b %Y %H:%M",     "%d %B %Y %H:%M"):
                try:
                    dt_obj = datetime.strptime(raw, fmt)
                    recorded_date     = dt_obj.strftime("%Y-%m-%d")
                    recorded_datetime = dt_obj.strftime("%Y-%m-%d %H:%M")
                    break
                except ValueError:
                    continue

        return {
            "datetime":        recorded_datetime,
            "date":            recorded_date,
            "source":          "kardia_pdf",
            "heart_rate_bpm":  hr,
            "determination":   determination,
            "status":          "✅ Normal" if "Normal" in determination else "⚠️ Review"
        }
    except Exception as e:
        st.error(f"PDF scan error: {e}")
        return None

# ─────────────────────────────────────────────
#  IMAGE EXTRACTION  – Fitdays scale via Claude Vision
# ─────────────────────────────────────────────
def extract_fitdays_image(file) -> dict | None:
    try:
        # Read and encode image as base64
        img_bytes = file.read()
        img_b64   = base64.standard_b64encode(img_bytes).decode("utf-8")

        # Detect media type
        img_obj    = Image.open(io.BytesIO(img_bytes))
        fmt        = (img_obj.format or "JPEG").lower()
        media_type = f"image/{'jpeg' if fmt in ('jpg','jpeg') else fmt}"

        # Call Claude vision API
        api_key = st.secrets.get("anthropic_api_key", "")
        if not api_key:
            st.error("Anthropic API key not found in secrets.")
            return None

        prompt = """This is a screenshot from the Fitdays body composition scale app.
Extract ALL of the following values exactly as shown. Return ONLY a JSON object with these keys
(use null if a value is not visible):
date (YYYY-MM-DD format, parsed from the timestamp shown),
weight_kg, bmi, body_fat_pct, fat_mass_kg, fat_free_weight_kg,
heart_rate_scale_bpm, cardiac_index,
muscle_mass_kg, muscle_rate_pct, skeletal_muscle_pct,
bone_mass_kg, protein_mass_kg, protein_pct,
water_weight_kg, body_water_pct, subcutaneous_fat_pct,
visceral_fat, bmr_kcal, body_age, ideal_weight_kg,
obesity_level, body_type.
Return only the JSON object, no explanation, no markdown."""

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-opus-4-5",
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64",
                                    "media_type": media_type,
                                    "data": img_b64}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            },
            timeout=30
        )

        if response.status_code != 200:
            st.error(f"Claude API error: {response.status_code}")
            return None

        raw_text = response.json()["content"][0]["text"].strip()
        # Strip markdown code fences if present
        raw_text = re.sub(r"^```json\s*|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
        data     = json.loads(raw_text)

        # Parse date
        date_str = data.get("date") or datetime.now().strftime("%Y-%m-%d")
        try:
            date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            date_str = datetime.now().strftime("%Y-%m-%d")

        # Auto-status using our benchmarks
        visceral = data.get("visceral_fat")
        body_fat = data.get("body_fat_pct")
        status_parts = []
        if visceral:  status_parts.append(vf_status(float(visceral)))
        if body_fat:  status_parts.append(bf_status(float(body_fat)))
        overall = "⚠️ Attention" if any("⚠️" in s for s in status_parts) else \
                  "🟡 Caution"   if any("🟡" in s for s in status_parts) else "✅ Good"

        return {
            "datetime":             date_str + " 00:00",
            "date":                 date_str,
            "source":               "fitdays_image",
            "weight_kg":            data.get("weight_kg"),
            "bmi":                  data.get("bmi"),
            "body_fat_pct":         data.get("body_fat_pct"),
            "fat_mass_kg":          data.get("fat_mass_kg"),
            "fat_free_weight_kg":   data.get("fat_free_weight_kg"),
            "heart_rate_scale_bpm": data.get("heart_rate_scale_bpm"),
            "cardiac_index":        data.get("cardiac_index"),
            "muscle_mass_kg":       data.get("muscle_mass_kg"),
            "muscle_rate_pct":      data.get("muscle_rate_pct"),
            "skeletal_muscle_pct":  data.get("skeletal_muscle_pct"),
            "bone_mass_kg":         data.get("bone_mass_kg"),
            "protein_mass_kg":      data.get("protein_mass_kg"),
            "protein_pct":          data.get("protein_pct"),
            "water_weight_kg":      data.get("water_weight_kg"),
            "body_water_pct":       data.get("body_water_pct"),
            "subcutaneous_fat_pct": data.get("subcutaneous_fat_pct"),
            "visceral_fat":         visceral,
            "bmr_kcal":             data.get("bmr_kcal"),
            "body_age":             data.get("body_age"),
            "ideal_weight_kg":      data.get("ideal_weight_kg"),
            "obesity_level":        data.get("obesity_level"),
            "body_type":            data.get("body_type"),
            "status":               overall
        }
    except Exception as e:
        st.error(f"Image scan error: {e}")
        return None

# ─────────────────────────────────────────────
#  LOAD DATA
# ─────────────────────────────────────────────
saved_height = load_profile()
df_logs      = load_logs()

# ─────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────
st.sidebar.markdown("## 🏥 Health Dashboard")
st.sidebar.caption(f"Patrick Kong · Age {age_today()} · {HEIGHT_CM} cm")
st.sidebar.markdown("---")

view_mode = st.sidebar.radio(
    "View",
    ["❤️ Cardiovascular", "⚖️ Body Composition", "📊 Full Summary"])

st.sidebar.markdown("---")

# ── File upload (no limit) ──
st.sidebar.markdown("### Upload Health Files")
st.sidebar.caption("Select scale screenshots or Kardia PDFs — they scan and save automatically.")

uploaded_files = st.sidebar.file_uploader(
    "Choose files",
    type=["pdf", "png", "jpg", "jpeg"],
    accept_multiple_files=True,
    label_visibility="collapsed"
)

if uploaded_files:
    results = []
    skipped = 0
    errors  = 0
    with st.sidebar:
        prog = st.progress(0, text="Scanning files…")
        for i, f in enumerate(uploaded_files):
            prog.progress((i + 1) / len(uploaded_files),
                          text=f"Scanning {f.name}…")
            if f.type == "application/pdf":
                row = extract_kardia_pdf(f)
            else:
                row = extract_fitdays_image(f)

            if row is None:
                errors += 1
                continue
            if is_duplicate(row, df_logs):
                skipped += 1
                continue
            if save_row(row, "logs"):
                results.append(row)

        prog.empty()

        if results:
            st.success(f"✅ {len(results)} file(s) saved to Google Sheet!")
            df_logs = load_logs()   # refresh display
        if skipped:
            st.info(f"ℹ️ {skipped} duplicate(s) skipped.")
        if errors:
            st.warning(f"⚠️ {errors} file(s) could not be read.")

st.sidebar.markdown("---")

# ── Manual blood pressure ──
st.sidebar.markdown("### Log Blood Pressure")
with st.sidebar.form(key="bp_form", clear_on_submit=True):
    log_date  = st.date_input("Date", datetime.now())
    sys_mmhg  = st.number_input("Systolic (mmHg)",  50,  250, 120, 1)
    dia_mmhg  = st.number_input("Diastolic (mmHg)", 30,  150,  80, 1)
    pulse_bpm = st.number_input("Pulse (BPM)",       30,  200,  70, 1)
    if st.form_submit_button("Save Blood Pressure"):
        status = bp_status(sys_mmhg, dia_mmhg)
        row = {
            "datetime":  log_date.strftime("%Y-%m-%d 00:00"),
            "date":      log_date.strftime("%Y-%m-%d"),
            "source":    "manual_bp",
            "sys_mmHg":  sys_mmhg,
            "dia_mmHg":  dia_mmhg,
            "pulse_bpm": pulse_bpm,
            "status":    status
        }
        if not is_duplicate(row, df_logs):
            if save_row(row, "logs"):
                st.success("✅ Saved!")
                df_logs = load_logs()
        else:
            st.info("ℹ️ Entry for this date already exists — skipped.")

st.sidebar.markdown("---")

# ── Height ──
st.sidebar.markdown("### Update Height")
with st.sidebar.form(key="profile_form"):
    new_height = st.number_input("Height (cm)", 100, 250, saved_height, 1)
    if st.form_submit_button("Save Height"):
        if save_row({"height_cm": new_height}, "profile"):
            st.success("✅ Height updated!")

# ─────────────────────────────────────────────
#  BENCHMARK REFERENCE BAR  (always visible)
# ─────────────────────────────────────────────
def benchmark_bar():
    b   = get_benchmarks()
    age = age_today()
    st.markdown(
        f"<div style='background:#1e1e1e;border-radius:8px;padding:10px 16px;"
        f"font-size:0.78rem;color:#ccc;margin-bottom:1rem'>"
        f"<b>Your benchmarks</b> (Male · Chinese · Age {age}) &nbsp;|&nbsp; "
        f"BP optimal: &lt;{b['bp']['optimal_sys']}/{b['bp']['optimal_dia']} mmHg &nbsp;|&nbsp; "
        f"BMI normal (Asian): {b['bmi']['normal_low']}–{b['bmi']['normal_high']} &nbsp;|&nbsp; "
        f"Visceral fat optimal: ≤{b['vf']['optimal']} &nbsp;|&nbsp; "
        f"Body fat normal: {b['bf']['normal_low']}–{b['bf']['normal_high']}%"
        f"</div>",
        unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  MAIN DASHBOARD
# ─────────────────────────────────────────────
st.markdown(f"# Patrick's Health Dashboard")
benchmark_bar()

if df_logs.empty:
    st.info("No data yet. Upload files or log blood pressure using the sidebar.")
    st.stop()

# Subsets
df_bp    = df_logs[df_logs["source"] == "manual_bp"].dropna(
               subset=["sys_mmHg", "dia_mmHg"]).copy()
df_ecg   = df_logs[df_logs["source"] == "kardia_pdf"].dropna(
               subset=["heart_rate_bpm"]).copy()
df_scale = df_logs[df_logs["source"] == "fitdays_image"].dropna(
               subset=["weight_kg"]).copy()

# ── KPI row ──
st.markdown("### Latest Readings")
k1, k2, k3, k4, k5 = st.columns(5)

with k1:
    if not df_bp.empty:
        r = df_bp.iloc[-1]
        val   = f"{int(float(r['sys_mmHg']))}/{int(float(r['dia_mmHg']))} mmHg"
        delta = bp_status(float(r["sys_mmHg"]), float(r["dia_mmHg"]))
        st.metric("Blood Pressure", val, delta)
    else:
        st.metric("Blood Pressure", "No data")

with k2:
    if not df_ecg.empty:
        r     = df_ecg.iloc[-1]
        val   = f"{int(float(r['heart_rate_bpm']))} BPM"
        delta = hr_status(float(r["heart_rate_bpm"]))
        st.metric("Heart Rate (ECG)", val, delta)
    else:
        st.metric("Heart Rate (ECG)", "No data")

with k3:
    if not df_scale.empty and pd.notna(df_scale.iloc[-1].get("weight_kg")):
        r     = df_scale.iloc[-1]
        val   = f"{float(r['weight_kg']):.1f} kg"
        delta = bmi_status(float(r["bmi"])) if pd.notna(r.get("bmi")) else ""
        st.metric("Weight", val, delta)
    else:
        st.metric("Weight", "No data")

with k4:
    if not df_scale.empty and pd.notna(df_scale.iloc[-1].get("visceral_fat")):
        r     = df_scale.iloc[-1]
        val   = f"{float(r['visceral_fat']):.1f}"
        delta = vf_status(float(r["visceral_fat"]))
        st.metric("Visceral Fat", val, delta)
    else:
        st.metric("Visceral Fat", "No data")

with k5:
    if not df_scale.empty and pd.notna(df_scale.iloc[-1].get("body_fat_pct")):
        r     = df_scale.iloc[-1]
        val   = f"{float(r['body_fat_pct']):.1f}%"
        delta = bf_status(float(r["body_fat_pct"]))
        st.metric("Body Fat", val, delta)
    else:
        st.metric("Body Fat", "No data")

st.markdown("---")

# ─────────────────────────────────────────────
#  VIEWS
# ─────────────────────────────────────────────
if "Cardiovascular" in view_mode:
    st.markdown("### ❤️ Cardiovascular")

    if not df_bp.empty:
        b = get_benchmarks()["bp"]
        fig = go.Figure()
        fig.add_scatter(x=df_bp["date"], y=df_bp["sys_mmHg"].astype(float),
                        name="Systolic", mode="lines+markers", line=dict(color="#e74c3c"))
        fig.add_scatter(x=df_bp["date"], y=df_bp["dia_mmHg"].astype(float),
                        name="Diastolic", mode="lines+markers", line=dict(color="#3498db"))
        fig.add_hline(y=b["high_sys"], line_dash="dash", line_color="#e74c3c",
                      annotation_text="High systolic", annotation_position="top left")
        fig.add_hline(y=b["optimal_sys"], line_dash="dot", line_color="#2ecc71",
                      annotation_text="Optimal systolic", annotation_position="top left")
        fig.update_layout(title="Blood Pressure Over Time", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No blood pressure data yet.")

    if not df_ecg.empty:
        b = get_benchmarks()["hr"]
        fig2 = px.bar(df_ecg, x="date", y="heart_rate_bpm",
                      color="determination",
                      title="Heart Rate – Kardia ECG Recordings",
                      labels={"heart_rate_bpm": "BPM", "date": "Date"})
        fig2.add_hline(y=b["optimal_low"],   line_dash="dot", line_color="#2ecc71")
        fig2.add_hline(y=b["optimal_high"],  line_dash="dot", line_color="#2ecc71",
                       annotation_text="Optimal range", annotation_position="top left")
        fig2.add_hline(y=b["caution_high"],  line_dash="dash", line_color="#f39c12",
                       annotation_text="Caution", annotation_position="top left")
        st.plotly_chart(fig2, use_container_width=True)

        # ECG table
        st.markdown("**ECG History**")
        show_cols = ["date", "heart_rate_bpm", "determination", "status"]
        show_cols = [c for c in show_cols if c in df_ecg.columns]
        st.dataframe(df_ecg[show_cols].sort_values("date", ascending=False)
                     .reset_index(drop=True), use_container_width=True)
    else:
        st.info("No ECG data yet.")

elif "Body Composition" in view_mode:
    st.markdown("### ⚖️ Body Composition")

    if df_scale.empty:
        st.info("No scale data yet.")
    else:
        b_bmi = get_benchmarks()["bmi"]
        b_vf  = get_benchmarks()["vf"]
        b_bf  = get_benchmarks()["bf"]

        col1, col2 = st.columns(2)

        with col1:
            fig = px.line(df_scale, x="date", y="weight_kg",
                          title="Weight (kg)", markers=True)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            if df_scale["bmi"].notna().any():
                fig = go.Figure()
                fig.add_scatter(x=df_scale["date"],
                                y=df_scale["bmi"].astype(float),
                                mode="lines+markers", name="BMI")
                fig.add_hline(y=b_bmi["normal_high"], line_dash="dot",
                              line_color="#2ecc71",
                              annotation_text="Overweight threshold (Asian 23)",
                              annotation_position="top left")
                fig.add_hline(y=b_bmi["obese"], line_dash="dash",
                              line_color="#e74c3c",
                              annotation_text="Obese threshold (Asian 27.5)",
                              annotation_position="top left")
                fig.update_layout(title="BMI Over Time")
                st.plotly_chart(fig, use_container_width=True)

        col3, col4 = st.columns(2)

        with col3:
            if df_scale["visceral_fat"].notna().any():
                fig = go.Figure()
                fig.add_scatter(x=df_scale["date"],
                                y=df_scale["visceral_fat"].astype(float),
                                mode="lines+markers", name="Visceral Fat",
                                line=dict(color="#e67e22"))
                fig.add_hline(y=b_vf["optimal"], line_dash="dot",
                              line_color="#2ecc71",
                              annotation_text=f"Optimal ≤{b_vf['optimal']} (Asian)",
                              annotation_position="top left")
                fig.add_hline(y=b_vf["caution"], line_dash="dash",
                              line_color="#f39c12",
                              annotation_text="Caution",
                              annotation_position="top left")
                fig.update_layout(title="Visceral Fat Rating")
                st.plotly_chart(fig, use_container_width=True)

        with col4:
            if df_scale["body_fat_pct"].notna().any():
                fig = go.Figure()
                fig.add_scatter(x=df_scale["date"],
                                y=df_scale["body_fat_pct"].astype(float),
                                mode="lines+markers", name="Body Fat %",
                                line=dict(color="#9b59b6"))
                fig.add_hline(y=b_bf["normal_high"], line_dash="dot",
                              line_color="#2ecc71",
                              annotation_text=f"Normal limit {b_bf['normal_high']}% (age {age_today()})",
                              annotation_position="top left")
                fig.update_layout(title="Body Fat %")
                st.plotly_chart(fig, use_container_width=True)

        # Extra metrics – split into 3 charts by scale
        st.markdown("**Other Body Metrics Over Time**")
        col5, col6 = st.columns(2)

        with col5:
            mass_cols = [c for c in ["muscle_mass_kg", "bone_mass_kg"]
                         if c in df_scale.columns and df_scale[c].notna().any()]
            if mass_cols:
                fig = px.line(df_scale, x="date", y=mass_cols, markers=True,
                              title="Muscle & Bone Mass (kg)",
                              labels={"value": "kg", "variable": "Metric"})
                st.plotly_chart(fig, use_container_width=True)

        with col6:
            pct_cols = [c for c in ["body_water_pct", "body_age"]
                        if c in df_scale.columns and df_scale[c].notna().any()]
            if pct_cols:
                fig = px.line(df_scale, x="date", y=pct_cols, markers=True,
                              title="Body Water % & Body Age",
                              labels={"value": "Value", "variable": "Metric"})
                st.plotly_chart(fig, use_container_width=True)

        if "bmr_kcal" in df_scale.columns and df_scale["bmr_kcal"].notna().any():
            fig = px.line(df_scale, x="date", y="bmr_kcal", markers=True,
                          title="Basal Metabolic Rate (kcal)",
                          labels={"bmr_kcal": "kcal"})
            fig.update_traces(line=dict(color="#e67e22"))
            st.plotly_chart(fig, use_container_width=True)

else:  # Full Summary
    st.markdown("### 📊 Full Summary")

    # Combined latest values table
    summary = {}
    if not df_bp.empty:
        r = df_bp.iloc[-1]
        summary["Blood Pressure"] = f"{int(float(r['sys_mmHg']))}/{int(float(r['dia_mmHg']))} mmHg"
        summary["BP Status"]      = bp_status(float(r["sys_mmHg"]), float(r["dia_mmHg"]))
    if not df_ecg.empty:
        r = df_ecg.iloc[-1]
        summary["Heart Rate (ECG)"] = f"{int(float(r['heart_rate_bpm']))} BPM"
        summary["ECG Determination"] = r.get("determination", "")
        summary["HR Status"]         = hr_status(float(r["heart_rate_bpm"]))
    if not df_scale.empty:
        r = df_scale.iloc[-1]
        for col, label in [
            ("weight_kg",          "Weight (kg)"),
            ("bmi",                "BMI"),
            ("body_fat_pct",       "Body Fat %"),
            ("visceral_fat",       "Visceral Fat"),
            ("muscle_mass_kg",     "Muscle Mass (kg)"),
            ("bone_mass_kg",       "Bone Mass (kg)"),
            ("body_water_pct",     "Body Water %"),
            ("bmr_kcal",           "BMR (kcal)"),
            ("body_age",           "Body Age"),
            ("ideal_weight_kg",    "Ideal Weight (kg)"),
            ("body_type",          "Body Type"),
        ]:
            if col in r and pd.notna(r[col]):
                summary[label] = r[col]
        if pd.notna(r.get("bmi")):
            summary["BMI Status"] = bmi_status(float(r["bmi"]))
        if pd.notna(r.get("visceral_fat")):
            summary["Visceral Fat Status"] = vf_status(float(r["visceral_fat"]))
        if pd.notna(r.get("body_fat_pct")):
            summary["Body Fat Status"] = bf_status(float(r["body_fat_pct"]))

    if summary:
        df_sum = pd.DataFrame(
            list(summary.items()), columns=["Metric", "Value"])
        st.dataframe(df_sum, use_container_width=True, hide_index=True)
    else:
        st.info("Upload data to see your full summary.")
