import io

import pandas as pd
import streamlit as st

from lookup import clean_domain, find_hq

st.set_page_config(page_title="HQ Postcode Finder", page_icon="📍", layout="wide")


def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


API_KEY = secret("GOOGLE_PLACES_API_KEY")
APP_PASSWORD = secret("APP_PASSWORD")

# ---------- password gate (only if APP_PASSWORD is set in Secrets) ----------
if APP_PASSWORD and not st.session_state.get("authed"):
    st.title("HQ Postcode Finder")
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pw == APP_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

# ---------- main page ----------
st.title("HQ Postcode Finder")
st.caption("Paste company domains and get each company's HQ address and postcode. "
           "Checks Google Maps first, then the brand's own terms, refund and contact pages.")

if not API_KEY:
    st.warning("No Google API key set up, so only the brand websites will be checked. "
               "Results will be patchier.")

region_label = st.radio("Where are the companies?", ["UK", "US", "Anywhere"], horizontal=True)
region = {"UK": "GB", "US": "US"}.get(region_label)

DOMAIN_COLS = ("domain", "website", "url", "company domain", "company website", "site")
BRAND_COLS = ("company", "company name", "brand", "brand name", "name", "store name")


def entries_from_csv(file):
    """Find the domain column by its header, or fall back to the first column."""
    df_in = pd.read_csv(file, dtype=str).fillna("")
    cols = {c.strip().lower(): c for c in df_in.columns}
    dcol = next((cols[c] for c in DOMAIN_COLS if c in cols), None)
    bcol = next((cols[c] for c in BRAND_COLS if c in cols), None)
    if dcol is None:  # no recognisable header: the header row itself may be a domain
        file.seek(0)
        df_in = pd.read_csv(file, header=None, dtype=str).fillna("")
        dcol = df_in.columns[0]
        bcol = None
    out = []
    for _, r in df_in.iterrows():
        d = clean_domain(r[dcol])
        if d and "." in d:
            out.append((d, r[bcol].strip() if bcol else ""))
    return list(dict.fromkeys(out))


single_tab, csv_tab = st.tabs(["One domain", "Upload a CSV"])

with single_tab:
    with st.form("single"):
        one = st.text_input("Company domain", placeholder="e.g. bloomandwild.com")
        one_brand = st.text_input("Brand name (optional, helps Google)",
                                  placeholder="e.g. Bloom & Wild")
        go_one = st.form_submit_button("Find postcode", type="primary")
    if go_one:
        d = clean_domain(one)
        if not d or "." not in d:
            st.error("Enter a domain like bloomandwild.com")
        else:
            with st.spinner(f"Looking up {d}…"):
                try:
                    res = find_hq(d, API_KEY or None, region, one_brand)
                except RuntimeError as e:
                    st.error(f"{e}. Check the API key in the app's Secrets.")
                    st.stop()
            if res["postcode"]:
                st.success(f"### {res['postcode']}")
                st.write(res["address"])
                st.caption(f"Source: {res['source']} · Confidence: {res['confidence']}"
                           + (f" · {res['notes']}" if res["notes"] else ""))
            else:
                st.warning("No postcode found. " + res["notes"])
            if res["check_link"]:
                st.markdown(f"[Check the source]({res['check_link']})")

with csv_tab:
    uploaded = st.file_uploader(
        "CSV of domains",
        type=["csv"],
        help="Uses a column called domain, website or url if there is one, otherwise the "
             "first column. A company/brand name column is used too if present.",
    )
    entries = entries_from_csv(uploaded) if uploaded is not None else []
    if uploaded is not None:
        st.write(f"**{len(entries)}** domain{'s' if len(entries) != 1 else ''} found in the file")
    run = st.button("Find postcodes", type="primary", disabled=not entries)

with csv_tab:
    if run:
        rows = []
        bar = st.progress(0.0, text="Starting…")
        for i, (domain, brand) in enumerate(entries, 1):
            bar.progress((i - 1) / len(entries), text=f"Looking up {domain} ({i} of {len(entries)})")
            try:
                rows.append(find_hq(domain, API_KEY or None, region, brand))
            except RuntimeError as e:
                bar.empty()
                st.error(f"{e}. Check the API key in the app's Secrets.")
                st.stop()
        bar.progress(1.0, text="Done")
        st.session_state.results = pd.DataFrame(rows)

    if "results" in st.session_state:
        df = st.session_state.results
        found = (df["postcode"] != "").sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Postcodes found", f"{found} of {len(df)}")
        c2.metric("Worth a manual check", int((df["confidence"] == "Check").sum()))
        c3.metric("Google lookups used", int(df["google_calls"].sum()))

        st.dataframe(
            df[["domain", "postcode", "address", "confidence", "source", "check_link", "notes"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "check_link": st.column_config.LinkColumn("Check", display_text="Open"),
                "confidence": st.column_config.TextColumn("Confidence",
                    help="High = taken from Google or the brand's legal pages. "
                         "Check = could be a shop or a partial address, worth a quick look."),
            },
        )

        buf = io.StringIO()
        df.drop(columns=["google_calls"]).to_csv(buf, index=False)
        st.download_button("Download CSV", buf.getvalue(), "hq_postcodes.csv", "text/csv")
