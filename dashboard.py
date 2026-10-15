from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st

from sklearn.model_selection import train_test_split
from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="Property Intelligence | PropPredict AI",
    page_icon="🏙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE = Path(__file__).resolve().parent
DATASET_PATH = BASE / "dataset" / "house_data.csv"
BG_PATH = BASE / "assets" / "apartment_background.jpg"

GOLD = "#f3c96a"
BLUE = "#7fa8d9"
GREEN = "#6fcf97"
RED = "#f2795a"
PURPLE = "#b98af3"
PALETTE = [GOLD, BLUE, GREEN, RED, PURPLE]


# =========================================================
# PREMIUM THEME
# =========================================================

@st.cache_data(show_spinner=False)
def get_bg_data_url(path: Path) -> str:
    if not path.exists():
        return ""
    import base64
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/jpeg;base64,{encoded}"


bg_url = get_bg_data_url(BG_PATH)

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    #MainMenu {{ visibility: hidden; }}
    footer {{ visibility: hidden; }}
    header {{ visibility: hidden; }}

    html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}

    .stApp {{ background: #07101d; }}

    .stApp::before {{
        content: "";
        position: fixed; inset: 0; z-index: -3;
        background-image: url("{bg_url}");
        background-size: cover; background-position: center;
        animation: slowZoom 18s ease-in-out infinite alternate;
        transform-origin: center;
    }}

    .stApp::after {{
        content: "";
        position: fixed; inset: 0; z-index: -2;
        background:
            radial-gradient(circle at 75% 20%, rgba(41, 127, 255, .16), transparent 34%),
            linear-gradient(90deg, rgba(3, 10, 19, .95) 0%, rgba(4, 12, 22, .78) 40%, rgba(4, 9, 17, .82) 100%);
    }}

    @keyframes slowZoom {{ 0% {{ transform: scale(1); }} 100% {{ transform: scale(1.10); }} }}
    @keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes glowPulse {{
        0% {{ box-shadow: 0 0 0px rgba(243,201,106,0); }}
        45% {{ box-shadow: 0 0 38px rgba(243,201,106,.35); }}
        100% {{ box-shadow: 0 0 0px rgba(243,201,106,0); }}
    }}
    @keyframes shimmerText {{ 0% {{ background-position: -200% center; }} 100% {{ background-position: 200% center; }} }}
    @keyframes growBar {{ from {{ width: 0%; }} to {{ width: var(--w, 0%); }} }}
    @keyframes popIn {{ from {{ opacity: 0; transform: scale(.94); }} to {{ opacity: 1; transform: scale(1); }} }}

    .block-container {{ max-width: 1450px; padding-top: 1.4rem; padding-bottom: 3rem; }}

    [data-testid="stSidebar"] {{
        background: rgba(4, 11, 21, .84);
        border-right: 1px solid rgba(255,255,255,.10);
        backdrop-filter: blur(20px);
    }}
    [data-testid="stSidebar"] * {{ color: #eef4ff !important; }}

    .hero {{
        padding: 30px 35px; border-radius: 24px; margin-bottom: 22px;
        background: linear-gradient(145deg, rgba(18, 30, 47, .76), rgba(6, 15, 27, .58));
        border: 1px solid rgba(255,255,255,.12);
        box-shadow: 0 24px 70px rgba(0,0,0,.28), inset 0 1px rgba(255,255,255,.05);
        backdrop-filter: blur(18px); animation: fadeUp .5s ease;
        transition: box-shadow .3s ease;
    }}
    .eyebrow {{ color: {GOLD}; font-size: 12px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 8px; }}
    .hero h1 {{ color: #f7f9fc; font-size: clamp(30px, 3.4vw, 46px); font-weight: 800; letter-spacing: -1.6px; margin: 0; }}
    .hero h1 span {{
        background: linear-gradient(90deg, #a97722, {GOLD}, #f7e3ad, {GOLD}, #a97722);
        background-size: 250% auto;
        -webkit-background-clip: text; background-clip: text; color: transparent;
        animation: shimmerText 5s linear infinite;
    }}
    .hero p {{ color: #b8c3d3; font-size: 15px; margin-top: 8px; }}

    .section-title {{ color: #f5f7fb; font-size: 21px; font-weight: 700; margin-top: 22px; margin-bottom: 4px; }}
    .section-sub {{ color: #8795aa; font-size: 12px; margin-bottom: 16px; }}

    .glass {{
        background: linear-gradient(145deg, rgba(18, 30, 47, .76), rgba(6, 15, 27, .58));
        border: 1px solid rgba(255,255,255,.12);
        box-shadow: 0 24px 70px rgba(0,0,0,.28), inset 0 1px rgba(255,255,255,.05);
        border-radius: 20px; padding: 20px;
        backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
        animation: fadeUp .55s ease;
        transition: transform .3s ease, box-shadow .3s ease;
    }}
    .glass:hover {{ transform: translateY(-4px); box-shadow: 0 30px 80px rgba(0,0,0,.35); }}

    .metric {{
        background: rgba(9,19,33,.74); border: 1px solid rgba(255,255,255,.10);
        border-radius: 18px; padding: 18px 19px; min-height: 108px;
        backdrop-filter: blur(14px); animation: fadeUp .5s ease both;
        border-left: 3px solid var(--accent, {GOLD});
        transition: transform .25s ease, box-shadow .25s ease;
    }}
    .metric:hover {{ transform: translateY(-4px) scale(1.01); box-shadow: 0 14px 34px rgba(0,0,0,.3); }}
    .metric .icon {{ font-size: 22px; }}
    .metric .num {{ color: {GOLD}; font-size: 26px; font-weight: 800; margin-top: 6px; }}
    .metric .lbl {{ color: #9eacc0; font-size: 12px; margin-top: 2px; }}

    .progress-track {{
        height: 6px; background: rgba(255,255,255,.1); border-radius: 4px;
        overflow: hidden; margin-top: 8px;
    }}
    .progress-fill {{
        height: 100%; border-radius: 4px;
        animation: growBar 1.1s cubic-bezier(.2,.8,.2,1) forwards;
    }}

    .price-card {{
        margin-top: 10px;
        background: linear-gradient(135deg, rgba(243,201,106,.15), rgba(20,61,100,.20));
        border: 1px solid rgba(243,201,106,.35); border-radius: 22px; padding: 24px; text-align: center;
        animation: popIn .45s cubic-bezier(.2,.8,.2,1) both, glowPulse 2s ease-out .15s 1;
    }}
    .price-title {{ color: #aab8ca; font-size: 12px; text-transform: uppercase; letter-spacing: 1.7px; }}
    .price-number {{ color: {GOLD}; font-size: 34px; font-weight: 800; margin: 6px 0; }}

    .rank-row {{
        display: flex; justify-content: space-between; align-items: center;
        background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.09);
        border-radius: 12px; padding: 10px 15px; margin-bottom: 8px; font-size: 13px; color: #dce5f2;
        transition: transform .2s ease, background .2s ease, border-color .2s ease;
        animation: fadeUp .4s ease both;
    }}
    .rank-row:hover {{ transform: translateX(4px); background: rgba(255,255,255,.07); border-color: rgba(243,201,106,.35); }}
    .rank-row .val {{ color: {GOLD}; font-weight: 700; }}

    label {{ color: #dce5f2 !important; font-weight: 600 !important; }}
    .stSelectbox div[data-baseweb="select"] > div {{
        background: rgba(4, 12, 22, .70) !important; color: #f4f7fb !important;
        border-color: rgba(255,255,255,.13) !important; border-radius: 12px !important;
    }}

    div[data-testid="stDataFrame"] {{ border-radius: 15px; overflow: hidden; }}

    button[data-baseweb="tab"] {{ transition: color .25s ease, transform .2s ease; }}
    button[data-baseweb="tab"]:hover {{ color: {GOLD} !important; transform: translateY(-2px); }}

    .footer {{ color: #75849a; text-align: center; padding: 30px 0 5px; font-size: 11px; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="Loading dataset…")
def load_dataset():
    d = pd.read_csv(DATASET_PATH)
    if "Area" in d.columns and "Price" in d.columns:
        d["Price_per_sqft"] = (d["Price"] * 100000) / d["Area"]
    return d


if not DATASET_PATH.exists():
    st.error("Dataset not found. Please keep dataset/house_data.csv in the project folder.")
    st.stop()

df = load_dataset()


def themed_fig(figsize):
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.tick_params(colors="#9eacc0")
    ax.xaxis.label.set_color("#c6d1df")
    ax.yaxis.label.set_color("#c6d1df")
    ax.title.set_color("#f5f7fb")
    for spine in ax.spines.values():
        spine.set_color("#374151")
    ax.grid(alpha=0.15, color="#9eacc0")
    return fig, ax


def metric_card(icon, value, label, accent=GOLD, delay=0.0):
    st.markdown(
        f"""
        <div class="metric" style="--accent:{accent}; animation-delay:{delay:.2f}s">
            <div class="icon">{icon}</div>
            <div class="num">{value}</div>
            <div class="lbl">{label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# HERO
# =========================================================

st.markdown(
    """
    <div class="hero">
        <div class="eyebrow">Data-driven insights</div>
        <h1>🏙️ Property <span>Intelligence</span></h1>
        <p>AI-powered house price analytics & machine learning dashboard.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# SIDEBAR FILTERS
# =========================================================

with st.sidebar:
    st.markdown("### 🎛️ Dashboard Filters")
    st.divider()

    selected_city = st.selectbox("🏙️ Select City", ["All Cities"] + sorted(df["City"].unique().tolist()))
    bhk_options = sorted(df["BHK"].unique().tolist())
    selected_bhk = st.multiselect("🛏️ BHK", bhk_options, default=bhk_options)
    price_min, price_max = float(df["Price"].min()), float(df["Price"].max())
    price_range = st.slider("💰 Price range (Lakh)", price_min, price_max, (price_min, price_max))

    filtered_df = df.copy()
    if selected_city != "All Cities":
        filtered_df = filtered_df[filtered_df["City"] == selected_city]
    if selected_bhk:
        filtered_df = filtered_df[filtered_df["BHK"].isin(selected_bhk)]
    filtered_df = filtered_df[(filtered_df["Price"] >= price_range[0]) & (filtered_df["Price"] <= price_range[1])]

    st.divider()
    st.markdown("### 📌 Current Selection")
    st.caption(f"**Properties:** {len(filtered_df)}")
    st.caption(f"**Areas:** {filtered_df['Area_Name'].nunique() if len(filtered_df) else 0}")
    st.caption(f"**Average:** ₹{filtered_df['Price'].mean():.2f} Lakh" if len(filtered_df) else "**Average:** —")

if filtered_df.empty:
    st.warning("Is filter combination ke liye koi property nahi mili. Filters adjust karo.")
    st.stop()


# =========================================================
# TABS
# =========================================================

tab_overview, tab_city, tab_property, tab_amenity, tab_ml, tab_records = st.tabs(
    ["📊 Overview", "🏙️ City & Locality", "📐 Property Analysis", "✨ Amenities", "🤖 ML Models", "📁 Records"]
)

# ---------------------------------------------------------
# OVERVIEW
# ---------------------------------------------------------
with tab_overview:
    st.markdown('<div class="section-title">Market Overview</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Key numbers for the current selection</div>', unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)
    with c1: metric_card("🏠", f"{len(filtered_df)}", "Total Properties", GOLD, delay=0.0)
    with c2: metric_card("📍", f"{filtered_df['Area_Name'].nunique()}", "Localities", BLUE, delay=0.08)
    with c3: metric_card("💰", f"₹{filtered_df['Price'].mean():.1f} L", "Average Price", GREEN, delay=0.16)
    with c4: metric_card("📈", f"₹{filtered_df['Price'].max():.1f} L", "Highest Price", RED, delay=0.24)

    st.write("")
    colA, colB = st.columns(2, gap="large")
    with colA:
        st.markdown('<div class="section-title" style="margin-top:0;">🏆 Top 5 Expensive Localities</div>', unsafe_allow_html=True)
        top5 = filtered_df.groupby("Area_Name")["Price"].mean().sort_values(ascending=False).head(5)
        for i, (name, val) in enumerate(top5.items()):
            st.markdown(f'<div class="rank-row" style="animation-delay:{i*0.06:.2f}s"><span>{name}</span><span class="val">₹{val:.1f} L</span></div>', unsafe_allow_html=True)
    with colB:
        st.markdown('<div class="section-title" style="margin-top:0;">💸 Top 5 Affordable Localities</div>', unsafe_allow_html=True)
        bottom5 = filtered_df.groupby("Area_Name")["Price"].mean().sort_values(ascending=True).head(5)
        for i, (name, val) in enumerate(bottom5.items()):
            st.markdown(f'<div class="rank-row" style="animation-delay:{i*0.06:.2f}s"><span>{name}</span><span class="val">₹{val:.1f} L</span></div>', unsafe_allow_html=True)

    if "Price_per_sqft" in filtered_df.columns:
        st.write("")
        st.markdown('<div class="section-title">💹 Price per sq.ft. by City</div>', unsafe_allow_html=True)
        pps = filtered_df.groupby("City")["Price_per_sqft"].mean().sort_values(ascending=False)
        fig, ax = themed_fig((10, 3.6))
        bars = ax.bar(pps.index, pps.values, color=PALETTE[:len(pps)])
        for b in bars:
            ax.annotate(f"₹{b.get_height():,.0f}", (b.get_x() + b.get_width() / 2, b.get_height()),
                        textcoords="offset points", xytext=(0, 5), ha="center", color="#e5ecf5", fontsize=9)
        ax.set_ylabel("Price per sq.ft. (₹)")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

# ---------------------------------------------------------
# CITY & LOCALITY
# ---------------------------------------------------------
with tab_city:
    st.markdown('<div class="section-title">🏙️ City-wise Price Analysis</div>', unsafe_allow_html=True)
    city_price = filtered_df.groupby("City")["Price"].mean().sort_values(ascending=False)
    fig, ax = themed_fig((10, 4))
    bars = ax.bar(city_price.index, city_price.values, color=PALETTE[:len(city_price)])
    for b in bars:
        ax.annotate(f"{b.get_height():.1f}", (b.get_x() + b.get_width() / 2, b.get_height()),
                    textcoords="offset points", xytext=(0, 5), ha="center", color="#e5ecf5", fontsize=9)
    ax.set_ylabel("Average Price (Lakh)")
    ax.set_xlabel("City")
    ax.set_title("Average Property Price by City")
    st.pyplot(fig, use_container_width=True, transparent=True)
    plt.close(fig)

    st.markdown('<div class="section-title">📍 Locality Price Intelligence</div>', unsafe_allow_html=True)
    area_price = filtered_df.groupby("Area_Name")["Price"].mean().sort_values(ascending=False)
    fig, ax = themed_fig((11, 8))
    norm = (area_price.values - area_price.values.min()) / (np.ptp(area_price.values) + 1e-9)
    colors = plt.cm.YlOrRd(0.3 + 0.6 * norm)
    ax.barh(area_price.index, area_price.values, color=colors)
    ax.set_xlabel("Average Price (Lakh)")
    ax.set_ylabel("Locality")
    ax.set_title("Locality-wise Average Property Price")
    ax.invert_yaxis()
    st.pyplot(fig, use_container_width=True, transparent=True)
    plt.close(fig)

# ---------------------------------------------------------
# PROPERTY ANALYSIS
# ---------------------------------------------------------
with tab_property:
    col1, col2 = st.columns(2)

    with col1:
        st.markdown('<div class="section-title">📐 Area vs Price (colored by BHK)</div>', unsafe_allow_html=True)
        fig, ax = themed_fig((7, 5))
        sc = ax.scatter(filtered_df["Area"], filtered_df["Price"], c=filtered_df["BHK"], cmap="cool", alpha=0.7, s=40)
        cbar = fig.colorbar(sc, ax=ax)
        cbar.set_label("BHK", color="#c6d1df")
        cbar.ax.yaxis.set_tick_params(color="#9eacc0")
        plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="#9eacc0")
        ax.set_xlabel("Property Area (sq.ft.)")
        ax.set_ylabel("Price (Lakh)")
        ax.set_title("Property Size vs Price")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    with col2:
        st.markdown('<div class="section-title">🛏️ Price Spread by BHK</div>', unsafe_allow_html=True)
        fig, ax = themed_fig((7, 5))
        groups = [filtered_df[filtered_df["BHK"] == b]["Price"].values for b in sorted(filtered_df["BHK"].unique())]
        bp = ax.boxplot(groups, labels=sorted(filtered_df["BHK"].unique()), patch_artist=True)
        for patch, color in zip(bp["boxes"], PALETTE * 3):
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
        for element in ["whiskers", "caps", "medians"]:
            plt.setp(bp[element], color="#cfd9e8")
        ax.set_xlabel("BHK")
        ax.set_ylabel("Price (Lakh)")
        ax.set_title("Price Distribution by BHK")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    st.markdown('<div class="section-title">🔗 Correlation Heatmap</div>', unsafe_allow_html=True)
    numeric_cols = [c for c in ["Area", "BHK", "Bathrooms", "Price", "Price_per_sqft"] if c in filtered_df.columns]
    corr = filtered_df[numeric_cols].corr()
    fig, ax = themed_fig((7, 5.5))
    im = ax.imshow(corr.values, cmap="RdYlGn", vmin=-1, vmax=1)
    ax.set_xticks(range(len(numeric_cols)))
    ax.set_yticks(range(len(numeric_cols)))
    ax.set_xticklabels(numeric_cols, rotation=40, ha="right")
    ax.set_yticklabels(numeric_cols)
    for i in range(len(numeric_cols)):
        for j in range(len(numeric_cols)):
            ax.text(j, i, f"{corr.values[i, j]:.2f}", ha="center", va="center",
                    color="black" if abs(corr.values[i, j]) < 0.6 else "white", fontsize=9)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.yaxis.set_tick_params(color="#9eacc0")
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="#9eacc0")
    ax.set_title("How features relate to Price")
    st.pyplot(fig, use_container_width=True, transparent=True)
    plt.close(fig)

# ---------------------------------------------------------
# AMENITIES
# ---------------------------------------------------------
with tab_amenity:
    st.markdown('<div class="section-title">✨ Amenity Price Impact</div>', unsafe_allow_html=True)

    amenities = ["Gym", "Swimming_Pool", "Cinema_Hall", "Gaming_Zone"]
    amenity_names = ["Gym", "Swimming Pool", "Cinema Hall", "Gaming Zone"]
    amenity_prices = [filtered_df[filtered_df[a] == 1]["Price"].mean() for a in amenities]
    amenity_counts = [int(filtered_df[a].sum()) for a in amenities]

    col1, col2 = st.columns(2)
    with col1:
        fig, ax = themed_fig((7, 4.2))
        bars = ax.bar(amenity_names, amenity_prices, color=PALETTE[:4])
        for b in bars:
            h = b.get_height()
            if not np.isnan(h):
                ax.annotate(f"₹{h:.1f}L", (b.get_x() + b.get_width() / 2, h),
                            textcoords="offset points", xytext=(0, 5), ha="center", color="#e5ecf5", fontsize=9)
        ax.set_ylabel("Average Price (Lakh)")
        ax.set_title("Avg Price WITH Each Amenity")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    with col2:
        fig, ax = themed_fig((7, 4.2))
        wedges, texts, autotexts = ax.pie(
            amenity_counts, labels=amenity_names, autopct="%1.0f%%",
            colors=PALETTE[:4], textprops={"color": "#e5ecf5", "fontsize": 9},
            wedgeprops={"edgecolor": "#07101d", "linewidth": 1.5},
        )
        ax.set_title("Share of Listings with Each Amenity", color="#f5f7fb")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

# ---------------------------------------------------------
# MACHINE LEARNING
# ---------------------------------------------------------
with tab_ml:
    st.markdown('<div class="section-title">🤖 Machine Learning Performance</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Trained once on the full dataset and cached for smooth reloads</div>', unsafe_allow_html=True)

    @st.cache_resource(show_spinner="Training models…")
    def train_models(data: pd.DataFrame):
        model_cols_source = [c for c in data.columns if c != "Price_per_sqft"]
        model_df = pd.get_dummies(data[model_cols_source], columns=["City", "Area_Name"], dtype=int)
        X = model_df.drop("Price", axis=1)
        y = model_df["Price"]
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, random_state=42)

        models = {
            "Linear Regression": LinearRegression(),
            "Decision Tree": DecisionTreeRegressor(random_state=42, max_depth=6),
            "Random Forest": RandomForestRegressor(n_estimators=100, random_state=42, max_depth=8),
        }

        results, preds, fitted = [], {}, {}
        for name, m in models.items():
            m.fit(X_train, y_train)
            pred = m.predict(X_test)
            mae = mean_absolute_error(y_test, pred)
            mse = mean_squared_error(y_test, pred)
            rmse = mse ** 0.5
            r2 = r2_score(y_test, pred)
            results.append([name, round(mae, 2), round(mse, 2), round(rmse, 2), round(r2, 2)])
            preds[name] = pred
            fitted[name] = m

        results_df = pd.DataFrame(results, columns=["Model", "MAE", "MSE", "RMSE", "R² Score"])
        return results_df, y_test, preds, fitted, X.columns.tolist()

    results_df, y_test, preds, fitted_models, feature_names = train_models(df)
    best_model_row = results_df.loc[results_df["R² Score"].idxmax()]
    best_model_name = best_model_row["Model"]

    m1, m2, m3 = st.columns(3)
    for i, (column, row, accent) in enumerate(zip([m1, m2, m3], results_df.values.tolist(), PALETTE)):
        with column:
            r2_pct = max(0, min(row[4], 1)) * 100
            st.markdown(
                f"""
                <div class="metric" style="--accent:{accent}; animation-delay:{i*0.1:.2f}s">
                    <div class="icon">🤖</div>
                    <div class="lbl">{row[0]}</div>
                    <div class="num">R² {row[4]}</div>
                    <div class="progress-track"><div class="progress-fill" style="--w:{r2_pct}%; background:{accent};"></div></div>
                    <div class="lbl">RMSE: {row[3]}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("##### 📋 Detailed Model Results")
    st.dataframe(results_df, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        fig, ax = themed_fig((6.5, 4))
        bars = ax.bar(results_df["Model"], results_df["R² Score"], color=PALETTE[:3])
        for b in bars:
            ax.annotate(f"{b.get_height():.2f}", (b.get_x() + b.get_width() / 2, b.get_height()),
                        textcoords="offset points", xytext=(0, 5), ha="center", color="#e5ecf5", fontsize=9)
        ax.set_ylabel("R² Score")
        ax.set_title("Model Performance Comparison")
        ax.set_ylim(0, 1)
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    with col2:
        st.markdown(f"**Actual vs Predicted — {best_model_name}**")
        fig, ax = themed_fig((6.5, 4))
        best_pred = preds[best_model_name]
        ax.scatter(y_test, best_pred, alpha=0.6, color=BLUE, s=30)
        lims = [min(y_test.min(), best_pred.min()), max(y_test.max(), best_pred.max())]
        ax.plot(lims, lims, color=GOLD, linestyle="--", linewidth=1.5)
        ax.set_xlabel("Actual Price (Lakh)")
        ax.set_ylabel("Predicted Price (Lakh)")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    rf_model = fitted_models.get("Random Forest")
    if rf_model is not None and hasattr(rf_model, "feature_importances_"):
        st.markdown('<div class="section-title">🔍 What Drives the Price? (Random Forest feature importance)</div>', unsafe_allow_html=True)
        importances = pd.Series(rf_model.feature_importances_, index=feature_names).sort_values(ascending=False).head(10)
        fig, ax = themed_fig((10, 4.5))
        norm = (importances.values - importances.values.min()) / (np.ptp(importances.values) + 1e-9)
        colors = plt.cm.plasma(0.25 + 0.65 * norm)
        ax.barh(importances.index[::-1], importances.values[::-1], color=colors[::-1])
        ax.set_xlabel("Importance")
        ax.set_title("Top 10 Most Influential Features")
        st.pyplot(fig, use_container_width=True, transparent=True)
        plt.close(fig)

    st.markdown(
        f"""
        <div class="price-card">
            <div class="price-title">🏆 Best Performing Model</div>
            <div class="price-number">{best_model_name}</div>
            <div class="lbl" style="color:#9eacc0;">R² Score: {best_model_row['R² Score']} &nbsp;&nbsp;|&nbsp;&nbsp; RMSE: {best_model_row['RMSE']} Lakh</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------
# RECORDS
# ---------------------------------------------------------
with tab_records:
    st.markdown('<div class="section-title">📁 Property Records</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Full dataset for the current filter selection</div>', unsafe_allow_html=True)

    search = st.text_input("🔎 Search by locality name", "")
    records_view = filtered_df[filtered_df["Area_Name"].str.contains(search, case=False, na=False)] if search else filtered_df

    sort_col = st.selectbox("Sort by", ["Price", "Area", "BHK", "Price_per_sqft"] if "Price_per_sqft" in records_view.columns else ["Price", "Area", "BHK"])
    sort_desc = st.checkbox("Descending", value=True)
    records_view = records_view.sort_values(sort_col, ascending=not sort_desc)

    st.dataframe(records_view, use_container_width=True, hide_index=True)

    st.download_button(
        "⬇️ Download filtered records as CSV",
        data=records_view.to_csv(index=False).encode("utf-8"),
        file_name=f"house_records_{selected_city.replace(' ', '_')}.csv",
        mime="text/csv",
    )

# =========================================================
# FOOTER
# =========================================================

st.markdown(
    """
    <div class="footer">
        🏠 <b>AI House Price Prediction</b><br>
        Machine Learning Minor Project<br><br>
        Synthetic development dataset • For academic demonstration
    </div>
    """,
    unsafe_allow_html=True,
)
