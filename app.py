import streamlit as st

# --- Pont de compatibilité st.cache (versions récentes de Streamlit) ---
if not hasattr(st, "cache"):
    def _legacy_cache_compat(func=None, **kwargs):
        for bad_kwarg in ("allow_output_mutation", "hash_funcs", "suppress_st_warning"):
            kwargs.pop(bad_kwarg, None)
        if func is not None:
            return st.cache_resource(func)
        return st.cache_resource(**kwargs)
    st.cache = _legacy_cache_compat

import json
import math
import re
import secrets as secrets_lib
import itertools
import hashlib
import requests
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from streamlit_cookies_manager import EncryptedCookieManager

# ============================================================
# CONFIGURATION PAGE
# ============================================================
st.set_page_config(page_title="Œil de l'Aigle", page_icon="🦅", layout="centered")

st.markdown("""
<style>
html, body, [class*="css"], .stMarkdown, .stText, p, span, div, label {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif !important;
    font-size: 16px !important;
    font-weight: 400 !important;
    line-height: 1.6 !important;
}
</style>
""", unsafe_allow_html=True)

# ============================================================
# CONSTANTES
# ============================================================
MAX_INFO_MATCHES = 5
MAX_POOL_SIMPLE_COMBO = 10
MAX_POOL_MULTI_AUTO = 15
MAX_MANUAL_GLOBAL = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.70
HIGH_ODDS_THRESHOLD = 15.0
HIGH_ODDS_CONFIDENCE = 0.60

PASSWORD_SALT = "oeildelaigle_salt_2024"

FOOTBALL_COMPETITIONS = {
    "Premier League": "PL",
    "Ligue 1": "FL1",
    "Liga": "PD",
    "Serie A": "SA",
    "Bundesliga": "BL1",
    "Ligue des Champions": "CL",
    "Championship": "ELC",
    "Eredivisie": "DED",
    "Primeira Liga": "PPL",
    "Brasileirão": "BSA",
}

MANUAL_MARKETS = {
    "Tennis": ["Vainqueur du match", "Total jeux Over/Under", "Handicap en jeux"],
    "Hockey sur glace": ["Vainqueur (incl. prolongation)", "Total buts Over/Under", "Puck line"],
    "Football américain": ["Vainqueur (Moneyline)", "Spread/Handicap", "Total points Over/Under"],
    "Football": ["1X2", "Double Chance", "BTTS", "Over/Under buts", "Total individuel équipe", "Handicap asiatique simple"],
    "Basketball": ["Victoire", "Handicap/Spread", "Over/Under points total", "Total individuel équipe"],
}

STAGE_LABELS = {
    "setup": "Configuration initiale",
    "manual_entry": "Saisie des matchs",
    "info_request": "Infos complémentaires",
    "options_ready": "Options générées / cotes en attente",
    "final": "Analyse terminée",
}

PERIOD_ORDER = ["Aujourd'hui", "Hier", "Cette semaine", "Ce mois-ci", "Plus ancien"]

NO_MARKDOWN_BASE = (
    "Réponds uniquement en texte brut, sans markdown, sans astérisques, sans titres, "
    "sans listes à puces, sans blocs de code, sans JSON visible, en phrases fluides "
    "et naturelles en français."
)

# ============================================================
# FORMULES DE CALIBRATION
# ============================================================
def estimated_selections(cote_cible):
    return max(2, round(math.log(cote_cible) / math.log(1.7)))

def validity_rate(cote_cible):
    return HIGH_ODDS_CONFIDENCE if cote_cible >= HIGH_ODDS_THRESHOLD else DEFAULT_CONFIDENCE_THRESHOLD

def recommended_manual_matches(cote_cible):
    sel = estimated_selections(cote_cible)
    rate = validity_rate(cote_cible)
    rec = math.ceil(sel / rate)
    return min(MAX_MANUAL_GLOBAL, max(sel, rec))

def determine_mode_params(cote_cible):
    if cote_cible <= 3.00:
        mode, coupon_max_size, jours, limit = "simple_double", 2, 2, MAX_POOL_SIMPLE_COMBO
    else:
        estimated = estimated_selections(cote_cible)
        if estimated <= 5:
            mode, coupon_max_size, jours, limit = "combo_info", estimated, 7, MAX_POOL_SIMPLE_COMBO
        else:
            mode, coupon_max_size, jours, limit = "multi", None, 7, MAX_POOL_MULTI_AUTO
    threshold = validity_rate(cote_cible)
    return mode, coupon_max_size, jours, limit, threshold

def ensure_default(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

# ============================================================
# GOOGLE SHEETS — CLIENT & UTILISATEURS
# ============================================================
@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_sheet(tab_name):
    client = get_gspread_client()
    sh = client.open_by_url(st.secrets["GOOGLE_SHEET_URL"])
    return sh.worksheet(tab_name)

def hash_password(password):
    return hashlib.sha256((password + PASSWORD_SALT).encode()).hexdigest()

def find_user_row(email):
    try:
        sheet = get_sheet("Users")
        records = sheet.get_all_values()
        for i, row in enumerate(records[1:], start=2):
            if row and row[0].strip().lower() == email.strip().lower():
                return i, row
        return None
    except Exception:
        return None

def create_user(email, password):
    sheet = get_sheet("Users")
    sheet.append_row([email.strip(), hash_password(password), datetime.now().isoformat(), "", ""])

def verify_login(email, password):
    result = find_user_row(email)
    if not result:
        return False
    _, row = result
    stored_hash = row[1] if len(row) > 1 else ""
    return stored_hash == hash_password(password)

def set_session_token(email, token):
    result = find_user_row(email)
    if not result:
        return
    row_index, _ = result
    sheet = get_sheet("Users")
    sheet.update_cell(row_index, 5, token)

def check_session_token(token):
    try:
        sheet = get_sheet("Users")
        records = sheet.get_all_values()
        for row in records[1:]:
            if len(row) >= 5 and token and row[4] == token:
                return row[0]
        return None
    except Exception:
        return None

def clear_session_token(email):
    result = find_user_row(email)
    if not result:
        return
    row_index, _ = result
    sheet = get_sheet("Users")
    sheet.update_cell(row_index, 5, "")

# ============================================================
# HISTORIQUE
# ============================================================
def classify_period(timestamp_str):
    try:
        ts = datetime.fromisoformat(timestamp_str)
    except Exception:
        return "Plus ancien"
    now = datetime.now()
    today = now.date()
    ts_date = ts.date()
    if ts_date == today:
        return "Aujourd'hui"
    elif ts_date == today - timedelta(days=1):
        return "Hier"
    elif ts_date >= today - timedelta(days=today.weekday()):
        return "Cette semaine"
    elif ts_date.year == today.year and ts_date.month == today.month:
        return "Ce mois-ci"
    else:
        return "Plus ancien"

def _prune_old_history(email, max_entries=50):
    try:
        sheet = get_sheet("History")
        records = sheet.get_all_values()
        rows_with_idx = [(i + 2, row) for i, row in enumerate(records[1:]) if row and row[0] == email]
        if len(rows_with_idx) > max_entries:
            to_delete = rows_with_idx[: len(rows_with_idx) - max_entries]
            for idx, _ in sorted(to_delete, key=lambda x: -x[0]):
                sheet.delete_rows(idx)
    except Exception:
        pass

def save_history_entry(email, stage, sport, cote_cible, session_data_dict):
    try:
        sheet = get_sheet("History")
        timestamp = datetime.now().isoformat()
        session_json = json.dumps(session_data_dict, ensure_ascii=False)
        sheet.append_row([email, timestamp, stage, sport, str(cote_cible), session_json])
        _prune_old_history(email)
    except Exception:
        pass

def get_user_history(email):
    try:
        sheet = get_sheet("History")
        records = sheet.get_all_values()
        result = []
        for i, row in enumerate(records[1:], start=2):
            if len(row) >= 6 and row[0] == email:
                try:
                    session_data = json.loads(row[5]) if row[5] else {}
                except Exception:
                    session_data = {}
                result.append({
                    "row_index": i,
                    "email": row[0],
                    "timestamp": row[1],
                    "stage": row[2],
                    "sport": row[3],
                    "cote_cible": row[4],
                    "session_data": session_data,
                })
        return result
    except Exception:
        return []

def delete_history_rows(row_indices):
    try:
        sheet = get_sheet("History")
        for idx in sorted(set(row_indices), reverse=True):
            sheet.delete_rows(idx)
        return True
    except Exception:
        return False

def delete_all_history(email):
    entries = get_user_history(email)
    row_indices = [e["row_index"] for e in entries]
    return delete_history_rows(row_indices)

def group_history_by_analysis(entries):
    by_analysis = {}
    for e in entries:
        aid = e["session_data"].get("analysis_id") or f"__no_id_{e['row_index']}"
        if aid not in by_analysis or e["timestamp"] > by_analysis[aid]["timestamp"]:
            by_analysis[aid] = e
    grouped = sorted(by_analysis.values(), key=lambda x: x["timestamp"], reverse=True)
    return grouped

def group_by_period(grouped_entries):
    periods = {p: [] for p in PERIOD_ORDER}
    for e in grouped_entries:
        period = classify_period(e["timestamp"])
        periods[period].append(e)
    return periods

def render_history_page():
    st.title("🕑 Historique de vos analyses")
    display_aerated(
        "Retrouvez ici toutes vos analyses passées, classées par période. Cliquez sur "
        "« Reprendre » pour continuer une analyse interrompue, ou supprimez celles dont "
        "vous n'avez plus besoin."
    )

    entries = get_user_history(st.session_state["user_email"])

    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("🗑️ Tout supprimer", use_container_width=True):
            st.session_state["confirm_delete_all"] = True

    if st.session_state.get("confirm_delete_all"):
        st.warning("Confirmer la suppression de TOUT l'historique ? Cette action est irréversible.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Oui, tout supprimer"):
                delete_all_history(st.session_state["user_email"])
                st.session_state["confirm_delete_all"] = False
                st.success("Historique entièrement supprimé.")
                st.rerun()
        with c2:
            if st.button("❌ Annuler"):
                st.session_state["confirm_delete_all"] = False
                st.rerun()

    if not entries:
        st.info("Aucune analyse enregistrée pour le moment.")
    else:
        grouped = group_history_by_analysis(entries)
        periods = group_by_period(grouped)

        for period_index, period_name in enumerate(PERIOD_ORDER):
            period_entries = periods[period_name]
            if not period_entries:
                continue
            st.markdown(f"### {period_name}")

            if st.button(f"🗑️ Supprimer le bloc « {period_name} »", key=f"del_block_{period_index}"):
                aids = {e["session_data"].get("analysis_id") for e in period_entries}
                rows_to_delete = []
                for raw in entries:
                    raw_aid = raw["session_data"].get("analysis_id")
                    if raw_aid is not None and raw_aid in aids:
                        rows_to_delete.append(raw["row_index"])
                    elif raw_aid is None and raw in period_entries:
                        rows_to_delete.append(raw["row_index"])
                delete_history_rows(rows_to_delete)
                st.success(f"Bloc « {period_name} » supprimé.")
                st.rerun()

            for e in period_entries:
                sport = e["sport"] or "—"
                cote = e["cote_cible"] or "—"
                stage_label = STAGE_LABELS.get(e["stage"], e["stage"])
                try:
                    dt_display = datetime.fromisoformat(e["timestamp"]).strftime("%d/%m/%Y à %H:%M")
                except Exception:
                    dt_display = e["timestamp"]

                with st.container():
                    st.markdown(f"**{sport}** — Cote cible : {cote} — *{stage_label}*")
                    st.caption(f"Dernière activité : {dt_display}")
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("▶️ Reprendre", key=f"resume_{e['row_index']}"):
                            st.info(
                                "La reprise automatique de cette analyse sera activée à la "
                                "prochaine étape. Merci de patienter encore un peu !"
                            )
                    with c2:
                        if st.button("🗑️ Supprimer", key=f"del_single_{e['row_index']}"):
                            aid = e["session_data"].get("analysis_id")
                            if aid:
                                rows_to_delete = [
                                    raw["row_index"] for raw in entries
                                    if raw["session_data"].get("analysis_id") == aid
                                ]
                            else:
                                rows_to_delete = [e["row_index"]]
                            delete_history_rows(rows_to_delete)
                            st.success("Analyse supprimée.")
                            st.rerun()
                    st.divider()

    if st.button("⬅️ Retour à l'application"):
        st.session_state["view"] = "main"
        st.rerun()

def snapshot_session_data():
    return {
        "analysis_id": st.session_state.get("current_analysis_id"),
        "mode": st.session_state.get("mode"),
        "coupon_max_size": st.session_state.get("coupon_max_size"),
        "jours_fenetre": st.session_state.get("jours_fenetre"),
        "pool_limit": st.session_state.get("pool_limit"),
        "threshold": st.session_state.get("threshold"),
        "competition": st.session_state.get("data_competition"),
        "cote_cible": st.session_state.get("data_cote_cible"),
        "nb_tout": st.session_state.get("data_nb_tout"),
        "is_auto": st.session_state.get("is_auto_current"),
        "match_pool": st.session_state.get("match_pool"),
        "generated_options": st.session_state.get("generated_options"),
        "data_manual_text": st.session_state.get("data_manual_text"),
        "data_info_text": st.session_state.get("data_info_text"),
        "data_cotes_text": st.session_state.get("data_cotes_text"),
        "cotes_text_saved": st.session_state.get("data_cotes_text"),
    }

def record_progress(stage):
    try:
        save_history_entry(
            st.session_state["user_email"],
            stage,
            st.session_state.get("data_sport"),
            st.session_state.get("data_cote_cible"),
            snapshot_session_data(),
        )
    except Exception:
        pass

# ============================================================
# API SPORTIVES
# ============================================================
def fetch_football_matches(competition_code, days_window, limit):
    try:
        today = datetime.now().date()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=days_window)).isoformat()
        headers = {"X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]}
        if competition_code:
            url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches"
        else:
            url = "https://api.football-data.org/v4/matches"
        params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("matches", [])
        pool = []
        for m in matches[:limit]:
            home = m.get("homeTeam", {}).get("name", "Équipe A")
            away = m.get("awayTeam", {}).get("name", "Équipe B")
            comp = m.get("competition", {}).get("name", "")
            date_str = m.get("utcDate", "")
            label = f"{home} vs {away} ({comp}, {date_str[:10]})"
            pool.append({"label": label, "raw": m})
        return pool
    except Exception as e:
        st.error(f"Erreur lors de la récupération des matchs de football : {e}")
        return []

def fetch_nba_matches(days_window, limit):
    try:
        today = datetime.now().date()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=days_window)).isoformat()
        headers = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}
        url = "https://api.balldontlie.io/v1/games"
        params = {"start_date": date_from, "end_date": date_to, "per_page": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        games = data.get("data", [])
        pool = []
        for g in games[:limit]:
            home = g.get("home_team", {}).get("full_name", "Équipe A")
            away = g.get("visitor_team", {}).get("full_name", "Équipe B")
            date_str = g.get("date", "")
            label = f"{home} vs {away} (NBA, {date_str[:10]})"
            pool.append({"label": label, "raw": g})
        return pool
    except Exception as e:
        st.error(f"Erreur lors de la récupération des matchs NBA : {e}")
        return []

# ============================================================
# IA GEMINI
# ============================================================
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
GEMINI_MODEL = "gemini-flash-lite-latest"

def call_gemini(prompt):
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"[Erreur IA temporaire: {e}]"

def build_options_prompt(match_label, sport):
    suggestions = MANUAL_MARKETS.get(sport)
    suggestion_text = (
        f" Inspire-toi si besoin de ces types de marchés habituels pour ce sport : {', '.join(suggestions)}."
        if suggestions else ""
    )
    return f"""{NO_MARKDOWN_BASE}
Tu es un analyste sportif expert en {sport}. Pour le match suivant : {match_label}, propose exactement 3 options de marché plausibles et réalistes (nommées A, B et C), chacune avec une probabilité estimée en pourcentage, basées sur le contexte réel de ce match (forme des équipes, enjeux, historique).{suggestion_text}
Réponds d'abord par un court paragraphe de présentation du match en 2-3 phrases.
Puis insère exactement le texte ###DATA_BLOCK### suivi d'un JSON strict de la forme:
{{"A": {{"marche": "...", "probabilite": 0.0}}, "B": {{"marche": "...", "probabilite": 0.0}}, "C": {{"marche": "...", "probabilite": 0.0}}}}
"""

def parse_options_response(text):
    try:
        parts = text.split("###DATA_BLOCK###", 1)
        narrative = parts[0].strip()
        data_part = parts[1].strip() if len(parts) > 1 else "{}"
        data_part = data_part.strip("` \n")
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        options = json.loads(data_part)
        return narrative, options
    except Exception:
        return text.strip(), {
            "A": {"marche": "Option A", "probabilite": 0.33},
            "B": {"marche": "Option B", "probabilite": 0.33},
            "C": {"marche": "Option C", "probabilite": 0.34},
        }

def build_reliability_prompt(matches):
    listing = "\n".join([f"- {m['label']}" for m in matches])
    return f"""{NO_MARKDOWN_BASE}
Voici une liste de matchs à venir :
{listing}
Classe ces matchs du plus prévisible/fiable au moins prévisible, en te basant sur la forme des équipes, les enjeux et le contexte réel.
Insère ###DATA_BLOCK### suivi d'un JSON strict de la forme {{"classement": ["libellé du match 1", "libellé du match 2", ...]}} listant TOUS les matchs du plus au moins fiable.
"""

def rank_matches_by_reliability(matches, keep_n):
    if len(matches) <= keep_n:
        return matches, 0
    prompt = build_reliability_prompt(matches)
    response = call_gemini(prompt)
    try:
        _, data_part = response.split("###DATA_BLOCK###", 1)
        data_part = data_part.strip().strip("` \n")
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        ranking = json.loads(data_part).get("classement", [])
        ordered = []
        for label in ranking:
            for m in matches:
                if m["label"] == label and m not in ordered:
                    ordered.append(m)
                    break
        for m in matches:
            if m not in ordered:
                ordered.append(m)
        kept = ordered[:keep_n]
        discarded_count = len(matches) - len(kept)
        return kept, discarded_count
    except Exception:
        kept = matches[:keep_n]
        return kept, len(matches) - len(kept)

def build_extra_eval_prompt(match_label, extra_market_name, sport):
    return f"""{NO_MARKDOWN_BASE}
Pour le match {match_label} en {sport}, évalue la probabilité réaliste du marché suivant proposé par l'utilisateur : {extra_market_name}.
Insère ###DATA_BLOCK### suivi d'un JSON strict {{"probabilite": 0.0}}.
"""

def evaluate_extra_market(match_label, extra_market_name, sport):
    prompt = build_extra_eval_prompt(match_label, extra_market_name, sport)
    response = call_gemini(prompt)
    try:
        _, data_part = response.split("###DATA_BLOCK###", 1)
        data_part = data_part.strip().strip("` \n")
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        return json.loads(data_part).get("probabilite", 0.0)
    except Exception:
        return 0.0

def build_justification_prompt(selected_summary, alternatives_summary, cote_cible, produit_cotes):
    return f"""{NO_MARKDOWN_BASE}
Rédige une justification en 3 à 4 phrases fluides, sans aucune liste ni accolade ni JSON, expliquant pourquoi le coupon suivant a été retenu : {selected_summary}.
Cote cible demandée : {cote_cible}, produit des cotes obtenu : {produit_cotes:.2f}.
Compare brièvement avec les autres options évaluées mais non retenues : {alternatives_summary}.
"""

def clean_justification_text(text):
    text = re.sub(r"[{}\[\]]", "", text)
    text = text.replace("###DATA_BLOCK###", "")
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"#+", "", text)
    return text.strip()

def display_aerated(text):
    if not text:
        return
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    for p in paragraphs:
        st.write(p)
        st.write("")

def find_missing_mids(text, required_mids):
    present = set(re.findall(r"(M\d+)\s*:", text))
    missing = [m for m in required_mids if m not in present]
    return missing

def parse_cotes_for_match(mid, cotes_text):
    pattern = rf"{mid}\s*:\s*(.*?)(?=\nM\d+\s*:|\nEXTRA\s*:|$)"
    m = re.search(pattern, cotes_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    segment = m.group(1).strip()
    cotes = {}
    for token in re.findall(r"([A-Za-z_]+)\s*=\s*([\d.,]+)", segment):
        name, val = token
        try:
            cotes[name.upper()] = float(val.replace(",", "."))
        except ValueError:
            pass
    return cotes

def parse_extra_for_match(mid, cotes_text):
    pattern = rf"{mid}.*?EXTRA\s*:\s*(.*?)(?=\nM\d+\s*:|$)"
    m = re.search(pattern, cotes_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    segment = m.group(1).strip()
    extra = {}
    for part in segment.split(","):
        if "=" in part:
            name, val = part.split("=", 1)
            name = name.strip()
            try:
                extra[name] = float(val.strip().replace(",", "."))
            except ValueError:
                pass
    return extra

def find_best_combination(validated_matches, cote_cible, max_size):
    best = None
    best_diff = None
    n = len(validated_matches)
    upper = min(max_size if max_size else n, n)
    for size in range(1, upper + 1):
        for combo in itertools.combinations(validated_matches, size):
            produit = 1.0
            for c in combo:
                produit *= c["chosen_cote"]
            diff = abs(produit - cote_cible)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = combo
    return best, best_diff

# ============================================================
# PORTAIL DE CONNEXION
# ============================================================
cookies = EncryptedCookieManager(prefix="oeildelaigle_", password=st.secrets["COOKIE_PASSWORD"])
if not cookies.ready():
    st.stop()

ensure_default("logged_in", False)
ensure_default("user_email", "")

if not st.session_state["logged_in"]:
    token = cookies.get("session_token")
    if token:
        email_from_token = check_session_token(token)
        if email_from_token:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = email_from_token

if not st.session_state["logged_in"]:
    st.markdown("<h2 style='text-align:center;'>🦅 Œil de l'Aigle</h2>", unsafe_allow_html=True)
    display_aerated("Connectez-vous ou créez un compte pour accéder à votre assistant d'analyse sportive personnel.")

    mode_login = st.radio("", ["Se connecter", "Créer un compte"], horizontal=True, label_visibility="collapsed")

    email_input = st.text_input("Compte Gmail", placeholder="votreadresse@gmail.com", key="login_email_input")
    password_input = st.text_input("Mot de passe", type="password", placeholder="Votre mot de passe", key="login_password_input")

    fields_filled = bool(email_input.strip()) and bool(password_input.strip())

    if mode_login == "Se connecter":
        if st.button("Se connecter", disabled=not fields_filled):
            if verify_login(email_input.strip(), password_input):
                new_token = secrets_lib.token_hex(16)
                set_session_token(email_input.strip(), new_token)
                cookies["session_token"] = new_token
                cookies.save()
                st.session_state["logged_in"] = True
                st.session_state["user_email"] = email_input.strip()
                st.rerun()
            else:
                st.error("Email ou mot de passe incorrect.")
        if not fields_filled:
            st.caption("Remplissez les deux champs pour continuer.")
    else:
        if st.button("Créer le compte", disabled=not fields_filled):
            if find_user_row(email_input.strip()):
                st.error("Ce compte existe déjà.")
            else:
                create_user(email_input.strip(), password_input)
                st.success("Compte créé ! Vous pouvez maintenant vous connecter.")
        if not fields_filled:
            st.caption("Remplissez les deux champs pour continuer.")

    st.stop()

# ============================================================
# INITIALISATION DE L'ÉTAT DE SESSION
# ============================================================
ensure_default("view", "main")
ensure_default("stage", "setup")
ensure_default("data_sport", "Football")
ensure_default("data_sport_prev", None)
ensure_default("data_competition", "Premier League")
ensure_default("data_cote_cible", 2.0)
ensure_default("data_nb_tout", 0)
ensure_default("data_manual_text", "")
ensure_default("data_info_text", "")
ensure_default("data_cotes_text", "")
ensure_default("match_pool", [])
ensure_default("generated_options", {})
ensure_default("current_analysis_id", None)
ensure_default("info_mids", [])
ensure_default("mode", None)
ensure_default("coupon_max_size", None)
ensure_default("jours_fenetre", None)
ensure_default("pool_limit", None)
ensure_default("threshold", DEFAULT_CONFIDENCE_THRESHOLD)
ensure_default("is_auto_current", False)
ensure_default("confirm_delete_all", False)

# ============================================================
# SIDEBAR
# ============================================================
with st.sidebar:
    st.write(f"Connecté : {st.session_state['user_email']}")
    if st.button("🕑 Historique", use_container_width=True):
        st.session_state["view"] = "history"
        st.rerun()
    if st.button("🚪 Se déconnecter", use_container_width=True):
        clear_session_token(st.session_state["user_email"])
        if "session_token" in cookies:
            del cookies["session_token"]
            cookies.save()
        st.session_state["logged_in"] = False
        st.session_state["user_email"] = ""
        st.rerun()

# ============================================================
# AFFICHAGE HISTORIQUE
# ============================================================
if st.session_state["view"] == "history":
    render_history_page()
    st.stop()

# ============================================================
# ÉTAPE : SETUP
# ============================================================
if st.session_state["stage"] == "setup":
    st.title("🦅 Œil de l'Aigle")
    display_aerated(
        "Bienvenue dans votre assistant d'analyse sportive. Œil de l'Aigle scrute les matchs "
        "à venir, croise les données réelles disponibles et vous aide à construire un coupon "
        "cohérent avec la cote que vous visez. Choisissez votre sport, votre compétition et "
        "votre cote cible : l'application s'occupe du reste, en toute transparence sur ses sources."
    )

    sport_options = ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football américain", "Tout"]
    sport_widget = st.selectbox(
        "Sport", sport_options,
        index=sport_options.index(st.session_state["data_sport"]) if st.session_state["data_sport"] in sport_options else 0,
        key="sport_select_widget",
    )

    competition_widget_value = st.session_state["data_competition"]
    if sport_widget == "Football":
        comp_options = list(FOOTBALL_COMPETITIONS.keys()) + ["Tout"]
        if competition_widget_value not in comp_options:
            competition_widget_value = comp_options[0]
        competition_widget = st.selectbox("Compétition", comp_options, index=comp_options.index(competition_widget_value), key="competition_select_widget")
        st.success("✅ Données vérifiées — récupération automatique des matchs réels.")
    elif sport_widget == "Basketball":
        comp_options = ["NBA (auto)", "Euroligue (manuel)"]
        if competition_widget_value not in comp_options:
            competition_widget_value = comp_options[0]
        competition_widget = st.selectbox("Compétition", comp_options, index=comp_options.index(competition_widget_value), key="competition_select_widget")
        if competition_widget == "NBA (auto)":
            st.success("✅ Données vérifiées — récupération automatique des matchs réels.")
        else:
            st.warning("⚠️ Mode manuel — vous décrirez vous-même les matchs.")
    else:
        competition_widget = None
        st.warning("⚠️ Mode manuel — vous décrirez vous-même les matchs.")

    st.session_state["data_competition"] = competition_widget

    cote_widget = st.number_input(
        "Cote cible", min_value=1.01, max_value=1500.0,
        value=float(st.session_state["data_cote_cible"]), step=0.1, key="cote_input_widget",
    )

    is_auto_preview = sport_widget == "Football" or (sport_widget == "Basketball" and competition_widget == "NBA (auto)")

    if not is_auto_preview:
        recommended = recommended_manual_matches(cote_widget)
        default_value = st.session_state["data_nb_tout"] if st.session_state["data_nb_tout"] else recommended
        nb_tout_widget = st.slider(
            "Nombre de matchs que vous allez décrire",
            min_value=1, max_value=MAX_MANUAL_GLOBAL,
            value=min(MAX_MANUAL_GLOBAL, max(1, default_value)),
            key="nb_tout_slider_widget",
        )
        display_aerated(
            f"Pour une cote cible de {cote_widget}, nous recommandons environ {recommended} "
            f"matchs afin de disposer d'assez d'options validées."
        )
        st.session_state["data_nb_tout"] = nb_tout_widget

    sport_changed = (
        st.session_state.get("data_sport_prev") is not None
        and st.session_state["data_sport_prev"] != sport_widget
    )
    has_match_data = bool(
        st.session_state["data_manual_text"].strip()
        or st.session_state["match_pool"]
        or st.session_state["data_info_text"].strip()
        or st.session_state["data_cotes_text"].strip()
    )
    if sport_changed and has_match_data:
        st.session_state["data_manual_text"] = ""
        st.session_state["data_info_text"] = ""
        st.session_state["data_cotes_text"] = ""
        st.session_state["match_pool"] = []
        st.session_state["generated_options"] = {}
        st.info("Le sport a changé : les matchs, infos et cotes précédemment saisis ont été réinitialisés.")

    st.session_state["data_sport"] = sport_widget
    st.session_state["data_cote_cible"] = cote_widget
    st.session_state["data_sport_prev"] = sport_widget

    if st.button("🚀 Lancer l'analyse", use_container_width=True):
        st.session_state["current_analysis_id"] = secrets_lib.token_hex(8)

        if sport_widget == "Football":
            mode, coupon_max_size, jours, limit, threshold = determine_mode_params(cote_widget)
            competition_code = None if competition_widget == "Tout" else FOOTBALL_COMPETITIONS.get(competition_widget)
            with st.spinner("Récupération des matchs réels en cours..."):
                pool_raw = fetch_football_matches(competition_code, jours, limit)
            pool = [{"mid": f"M{i+1}", "label": p["label"], "raw": p["raw"]} for i, p in enumerate(pool_raw)]
            st.session_state["match_pool"] = pool
            st.session_state["mode"] = mode
            st.session_state["coupon_max_size"] = coupon_max_size
            st.session_state["jours_fenetre"] = jours
            st.session_state["pool_limit"] = limit
            st.session_state["threshold"] = threshold
            st.session_state["is_auto_current"] = True

            if mode in ("simple_double", "combo_info") and pool:
                kept, discarded_count = rank_matches_by_reliability(pool, MAX_INFO_MATCHES)
                st.session_state["info_mids"] = [m["mid"] for m in kept]
                st.session_state["discarded_count"] = discarded_count
                st.session_state["stage"] = "info_request"
                record_progress("info_request")
            else:
                st.session_state["info_mids"] = []
                st.session_state["stage"] = "options_ready"
                record_progress("options_ready")
            st.rerun()

        elif sport_widget == "Basketball" and competition_widget == "NBA (auto)":
            mode, coupon_max_size, jours, limit, threshold = determine_mode_params(cote_widget)
            with st.spinner("Récupération des matchs NBA réels en cours..."):
                pool_raw = fetch_nba_matches(jours, limit)
            pool = [{"mid": f"M{i+1}", "label": p["label"], "raw": p["raw"]} for i, p in enumerate(pool_raw)]
            st.session_state["match_pool"] = pool
            st.session_state["mode"] = mode
            st.session_state["coupon_max_size"] = coupon_max_size
            st.session_state["jours_fenetre"] = jours
            st.session_state["pool_limit"] = limit
            st.session_state["threshold"] = threshold
            st.session_state["is_auto_current"] = True

            if mode in ("simple_double", "combo_info") and pool:
                kept, discarded_count = rank_matches_by_reliability(pool, MAX_INFO_MATCHES)
                st.session_state["info_mids"] = [m["mid"] for m in kept]
                st.session_state["discarded_count"] = discarded_count
                st.session_state["stage"] = "info_request"
                record_progress("info_request")
            else:
                st.session_state["info_mids"] = []
                st.session_state["stage"] = "options_ready"
                record_progress("options_ready")
            st.rerun()

        else:
            st.session_state["threshold"] = validity_rate(cote_widget)
            st.session_state["mode"] = "manuel"
            st.session_state["coupon_max_size"] = None
            st.session_state["is_auto_current"] = False
            st.session_state["info_mids"] = []
            st.session_state["stage"] = "manual_entry"
            record_progress("manual_entry")
            st.rerun()

# ============================================================
# ÉTAPE : MANUAL_ENTRY
# ============================================================
elif st.session_state["stage"] == "manual_entry":
    st.title("✍️ Saisie manuelle des matchs")
    display_aerated(
        f"Puisque ce sport ne bénéficie pas encore d'une connexion automatique aux données "
        f"en direct, vous allez décrire vous-même les matchs à analyser. Indiquez jusqu'à "
        f"{st.session_state['data_nb_tout']} rencontres, chacune précédée de son identifiant "
        f"(M1, M2, etc.), par exemple : M1: PSG vs OM."
    )
    st.warning("⚠️ Mode manuel — vous êtes responsable de l'exactitude des informations saisies.")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    manual_widget = st.text_area(
        "Vos matchs", value=st.session_state["data_manual_text"], height=200,
        key="manual_text_widget", placeholder="M1: PSG vs OM\nM2: Real Madrid vs Barcelone",
    )
    st.session_state["data_manual_text"] = manual_widget

    required_mids = [f"M{i}" for i in range(1, st.session_state["data_nb_tout"] + 1)]
    missing = find_missing_mids(manual_widget, required_mids)

    if missing:
        st.warning(f"Il manque les identifiants suivants : {', '.join(missing)}")

    if st.button("Valider les matchs saisis", disabled=bool(missing) or not manual_widget.strip()):
        pool = []
        for mid in required_mids:
            pattern = rf"{mid}\s*:\s*(.*)"
            m = re.search(pattern, manual_widget)
            label = m.group(1).split("\n")[0].strip() if m else mid
            pool.append({"mid": mid, "label": label, "raw": None})
        st.session_state["match_pool"] = pool
        st.session_state["stage"] = "info_request"
        record_progress("info_request")
        st.rerun()

# ============================================================
# ÉTAPE : INFO_REQUEST
# ============================================================
elif st.session_state["stage"] == "info_request":
    st.title("🔎 Informations complémentaires")
    display_aerated(
        "Avant de générer les options d'analyse, prenez quelques minutes pour aller chercher "
        "des informations fraîches sur ces rencontres (compositions probables, blessures, "
        "forme récente, enjeux). Des sources comme Score24, Flashscore ou les sites officiels "
        "des compétitions sont idéales pour ça. Collez ensuite ici ce que vous avez trouvé, "
        "pour chaque match concerné."
    )

    info_mids = st.session_state.get("info_mids")
    if not info_mids:
        info_mids = [m["mid"] for m in st.session_state["match_pool"]]

    relevant_matches = [m for m in st.session_state["match_pool"] if m["mid"] in info_mids]

    st.write("Matchs concernés par cette demande d'informations :")
    for m in relevant_matches:
        st.write(f"- {m['mid']} : {m['label']}")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    info_widget = st.text_area(
        "Vos informations complémentaires", value=st.session_state["data_info_text"], height=220,
        key="info_text_widget",
        placeholder="M1: Compositions probables confirmées, attaquant titulaire incertain...\nM2: ...",
    )
    st.session_state["data_info_text"] = info_widget

    if st.button("Continuer vers la génération des options"):
        st.session_state["stage"] = "options_ready"
        record_progress("options_ready")
        st.rerun()

# ============================================================
# ÉTAPE : OPTIONS_READY
# ============================================================
elif st.session_state["stage"] == "options_ready":
    st.title("📊 Options d'analyse")
    display_aerated(
        "Voici, pour chaque match retenu, trois options de marché plausibles accompagnées de "
        "leur probabilité estimée. Rendez-vous ensuite sur votre site de paris favori pour "
        "récupérer les cotes réelles correspondantes, puis saisissez-les ci-dessous au format demandé."
    )

    match_pool = st.session_state["match_pool"]

    if not st.session_state["generated_options"]:
        options_store = {}
        with st.spinner("Génération des options en cours, cela peut prendre quelques minutes..."):
            for m in match_pool:
                prompt = build_options_prompt(m["label"], st.session_state["data_sport"])
                response = call_gemini(prompt)
                narrative, options = parse_options_response(response)
                options["_narrative"] = narrative
                options_store[m["mid"]] = options
        st.session_state["generated_options"] = options_store

    for m in match_pool:
        opts = st.session_state["generated_options"].get(m["mid"], {})
        st.subheader(f"{m['mid']} — {m['label']}")
        narrative = opts.get("_narrative", "")
        if narrative:
            display_aerated(narrative)
        for key in ["A", "B", "C"]:
            if key in opts:
                st.write(f"Option {key} : {opts[key].get('marche')} — probabilité estimée {opts[key].get('probabilite', 0)*100:.0f}%")
        st.divider()

    st.markdown("Format attendu pour chaque match : **M1: A=1.85 B=1.70 C=1.90**")
    st.markdown("Marché optionnel supplémentaire : **EXTRA: Nom_du_marche=cote,Autre_marche=cote**")

    cotes_widget = st.text_area(
        "Cotes réelles", value=st.session_state["data_cotes_text"], height=220,
        key="cotes_text_widget", placeholder="M1: A=1.85 B=1.70 C=1.90\nEXTRA: Both_teams_to_score=1.95",
    )
    st.session_state["data_cotes_text"] = cotes_widget

    required_mids = [m["mid"] for m in match_pool]
    missing = find_missing_mids(cotes_widget, required_mids)
    if missing:
        st.warning(f"Il manque les cotes pour : {', '.join(missing)}")

    if st.button("🏁 Finaliser l'analyse", disabled=bool(missing) or not cotes_widget.strip()):
        st.session_state["stage"] = "final"
        record_progress("final")
        st.rerun()

# ============================================================
# ÉTAPE : FINAL
# ============================================================
elif st.session_state["stage"] == "final":
    st.title("🏆 Résultat de l'analyse")
    display_aerated(
        "Voici le fruit de l'analyse : la combinaison qui se rapproche le plus de votre cote "
        "cible, accompagnée d'une explication claire de ce choix."
    )

    match_pool = st.session_state["match_pool"]
    generated_options = st.session_state["generated_options"]
    cotes_text = st.session_state["data_cotes_text"]
    threshold = st.session_state.get("threshold", DEFAULT_CONFIDENCE_THRESHOLD)
    cote_cible = st.session_state["data_cote_cible"]
    coupon_max_size = st.session_state.get("coupon_max_size")

    evaluated = []
    for m in match_pool:
        mid = m["mid"]
        options = generated_options.get(mid, {})
        cotes = parse_cotes_for_match(mid, cotes_text)
        extra = parse_extra_for_match(mid, cotes_text)

        candidates = []
        for key in ["A", "B", "C"]:
            if key in options and key in cotes:
                candidates.append({
                    "marche": options[key].get("marche", key),
                    "probabilite": options[key].get("probabilite", 0.0),
                    "cote": cotes[key],
                })
        for name, cote_val in extra.items():
            prob = evaluate_extra_market(m["label"], name, st.session_state["data_sport"])
            candidates.append({"marche": name, "probabilite": prob, "cote": cote_val})

        if not candidates:
            evaluated.append({
                "label": m["label"], "mid": mid, "statut": "Aucune cote fournie",
                "chosen_market": None, "chosen_prob": None, "chosen_cote": None,
            })
            continue

        best_candidate = max(candidates, key=lambda c: c["probabilite"])
        validated = best_candidate["probabilite"] >= threshold
        evaluated.append({
            "label": m["label"], "mid": mid,
            "chosen_market": best_candidate["marche"],
            "chosen_prob": best_candidate["probabilite"],
            "chosen_cote": best_candidate["cote"],
            "statut": "Validé" if validated else "Rejeté (sous le seuil)",
        })

    validated_matches = [e for e in evaluated if e["statut"] == "Validé"]

    if not validated_matches:
        st.warning(
            f"Aucun match n'a atteint le seuil de fiabilité requis ({int(threshold*100)}%). "
            f"Aucun coupon ne peut être proposé pour le moment. Essayez avec d'autres matchs "
            f"ou une autre cote cible."
        )
    else:
        max_size = coupon_max_size if coupon_max_size else min(len(validated_matches), 10)
        best_combo, best_diff = find_best_combination(validated_matches, cote_cible, max_size)

        if best_combo:
            produit = 1.0
            for c in best_combo:
                produit *= c["chosen_cote"]

            st.subheader("🎟️ Coupon recommandé")
            for c in best_combo:
                st.write(f"{c['label']} — {c['chosen_market']} (probabilité estimée {c['chosen_prob']*100:.0f}%, cote {c['chosen_cote']})")
            st.write(f"Produit des cotes : {produit:.2f} (cote cible demandée : {cote_cible})")

            selected_summary = ", ".join([f"{c['label']} ({c['chosen_market']})" for c in best_combo])
            not_selected = [e for e in validated_matches if e not in best_combo]
            alternatives_summary = (
                ", ".join([f"{e['label']} ({e['chosen_market']})" for e in not_selected])
                if not_selected else "aucune autre option validée"
            )

            justification_prompt = build_justification_prompt(selected_summary, alternatives_summary, cote_cible, produit)
            justification_raw = call_gemini(justification_prompt)
            justification = clean_justification_text(justification_raw)

            st.subheader("📝 Justification")
            display_aerated(justification)
        else:
            st.warning("Impossible de déterminer une combinaison satisfaisante.")

    with st.expander("📋 Récapitulatif complet de tous les matchs testés"):
        for e in evaluated:
            cote_display = e["chosen_cote"] if e["chosen_cote"] is not None else "—"
            prob_display = f"{e['chosen_prob']*100:.0f}%" if e["chosen_prob"] is not None else "—"
            marche_display = e["chosen_market"] or "—"
            st.write(f"{e['label']} — Marché retenu : {marche_display} — Cote : {cote_display} — Probabilité : {prob_display} — Statut : {e['statut']}")

    if st.button("🔄 Nouvelle analyse"):
        preserved = {"logged_in": st.session_state["logged_in"], "user_email": st.session_state["user_email"]}
        st.session_state.clear()
        for k, v in preserved.items():
            st.session_state[k] = v
        st.rerun()
